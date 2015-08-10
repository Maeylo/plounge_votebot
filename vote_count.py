#!/usr/bin/env python2.7

import os
import re
import praw
import json
import copy
import time
import pytz
import creds
import config
import os.path
import logging
import argparse
import requests
import datetime
import traceback
import prettylog
import collections
import praw.objects
import simpletemplate

from HTMLParser import HTMLParser

l = prettylog.ColoredLogger(__name__)

debug_levels = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG
}

parser = argparse.ArgumentParser(description="Plounge mafia vote counting bot")
parser.add_argument("--log_level", help="Log level to use", choices =
                     debug_levels.keys(), default = 'info')
parser.add_argument("--oneshot", action='store_true', help="run state transition once")
parser.add_argument("--update_delay", type=int, default=5, help="time in minutes between state updates")
parser.add_argument("--dry-run", action='store_true', help="Don't actually post anything")
parser.add_argument("--oauth-login", action='store_true', help="perform Oauth login")

Vote = collections.namedtuple("Vote", ["by", "target", "time"])
Nomination = collections.namedtuple('Nomination', ['player', 'yays', 'nays', 'up_for_trial', 'vote_post_id', 'timestamp'])

def Tree(dict_ = {}):
    tree = collections.defaultdict(Tree)
    tree.update(dict_)
    return tree

def chunk(l,n):
    for i in range(0,len(l),n):
        yield l[i:i+n]

known_dead_comments = set()

#replace_more_comments is broken because MoreComments.comments() is broken.
#This is broken I think because the reddit API is broken and doesn't return an
#additional morecomments object when it should. This also affects the website
def get_more_comments(self, update = True):
    if self._comments is not None:
        return self._comments

    children = {x for x in self.children if 't1_{}'.format(x)
                    not in self.submission._comments_by_id}

    self._comments = []
    if not children:
        return self._comments

    n_attempts = 0
    old_len = len(children)
    while children:
        data = {'children': ','.join(children),
                'link_id': self.submission.fullname,
                'r': str(self.submission.subreddit)}


        if self.submission._comment_sort:
            data['where'] = self.submission._comment_sort

        url = self.reddit_session.config['morechildren']
        response = self.reddit_session.request_json(url, data = data)
        self._comments.extend(response['data']['things'])
        children.difference_update(set([x.id for x in self._comments]))
        n_attempts += 1
        if n_attempts > 10 or old_len == len(children):
            if not children.issubset(known_dead_comments):
                l.error("Could not fetch comments {} after {} attempts".format(children, n_attempts))
                known_dead_comments.update(children)
            break
        old_len = len(children)

    if update:
        for comment in self._comments:
            comment._update_submission(self.submission)

    return self._comments

#replace_more_comments is broken (plus makes more requests than we need)
def all_comments(replies):
    more_comments = []
    for reply in replies:
        if isinstance(reply, praw.objects.MoreComments):
            more_comments.append(reply)
        else:
            yield reply
    while more_comments:
        more = more_comments.pop()
        maybe_more = get_more_comments(more)
        for additional_comment in get_more_comments(more):
            if isinstance(additional_comment, praw.objects.MoreComments):
                more_comments.append(additional_comment)
            else:
                yield additional_comment

nominate_re = re.compile("""
(nominate|vote|lynch)?         #vote or nominate is for clarity only, they have the same effect
\s*:?\s*                       #could be a colon or not
(/u/)?                         #might start with /u/
(?P<user>no\s*lynch|[^.*~\s]+) #username may consist of any characters except whitespace and *
                                               #'no lynch' is valid in traditional games, and contains a space
""", re.VERBOSE)

vote_re = re.compile("""
(vote)?:?                 #can start with vote or not
\s*
(?P<vote>
 yay|lynch|yes|second|    #many yes or no options. Must be synced with
 nay|pardon|no)           # get_vote_from_post()
""", re.VERBOSE)

class RedditHTMLParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.nest_count = collections.defaultdict(int)
        self.possible_votes = []

    def handle_starttag(self, tag, attrs):
        self.nest_count[tag] += 1

    def handle_endtag(self, tag):
        self.nest_count[tag] -= 1

    def handle_data(self, data):
        if self.nest_count["strong"] > 0 and self.nest_count["del"] == 0:
            self.possible_votes.append(data)

def get_possible_votes(post_contents):
    parser = RedditHTMLParser()
    parser.feed(parser.unescape(post_contents))
    return parser.possible_votes

def get_nomination_from_post(post_contents, valid_names):
    valid_votes = []
    for possible_vote in get_possible_votes(post_contents):
        for match in nominate_re.finditer(possible_vote.lower().strip()):
            if not match.group('user'):
                continue
            username = match.group('user').strip().lower()
            if username in valid_names:
                valid_votes.append(username)
    if valid_votes:
        return valid_votes[-1]

def get_vote_from_post(post_contents):
    valid_votes = []
    for possible_vote in get_possible_votes(post_contents):
        for match in vote_re.finditer(possible_vote.lower().strip()):
            if not match.group('vote'):
                continue
            vote = match.group('vote').strip().lower()
            if vote in ('yay', 'lynch', 'yes', 'second'):
                valid_votes.append(True)
            elif vote in ('nay', 'pardon', 'no'):
                valid_votes.append(False)
    if valid_votes:
        return valid_votes[-1]

def compare_dicts(old, new):
    old_items = set([(k,) + tuple(v.items()) for k,v in old.iteritems()])
    new_items = set([(k,) + tuple(v.items()) for k,v in new.iteritems()])

    additions = new_items.difference(old_items)
    removals = old_items.difference(new_items)

    additions = {i[0]: dict(i[1:]) for i in additions}
    removals = {i[0]: dict(i[1:]) for i in removals}

    return additions, removals

def get_edited_time(comment):
    return comment.edited if comment.edited else comment.created_utc

def timestamp_to_date(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, pytz.utc).isoformat()

class VoteBot(object):
    def __init__(self, reddit, credentials, args):
        self.bot_username = credentials.bot_username
        self.bot_password = credentials.bot_password
        self.authorized_users = args.authorized_users
        self.known_invalid_votes = set()
        self.state = Tree()
        self.reddit = reddit
        self.args = args
        self.max_trials = 5

    def setup_dir(self):
        if not os.path.exists(self.args.output_dir):
            os.makedirs(self.args.output_dir)

    def process_commands(self):
        l.debug("Processing commands for {}".format(self.args.name))
        new_state = copy.deepcopy(self.state)
        pms = self.reddit.get_inbox(limit = None)
        have_nominations = False
        have_votes = False
        most_recent_id = None
        alive_players = set(new_state["alive_players"])
        dead_players = set(new_state["dead_players"])
        voteless_players = set(new_state["voteless_players"])
        voteless_players.difference_update(dead_players)
        alive_players.difference_update(dead_players)
        pms_reversed = []
        for pm in pms:
            if ":" not in pm.subject:
                continue
            game_name, command = pm.subject.split(':')
            game_name = game_name.lower().strip()
            command = command.lower().strip()
            if not (game_name == self.args.name.lower() or game_name == "*"):
                continue
            if pm.id == self.state['most_recent_pm_id']:
                break
            if not most_recent_id:
                most_recent_id = pm.id
            if pm.author.name.lower() not in self.authorized_users:
                continue
            pms_reversed.append(pm)
            if command == "reset":
                break

        pms_reversed.reverse()
        for pm in pms_reversed:
            game_name, command = pm.subject.split(':')
            game_name = game_name.lower().strip()
            command = command.lower().strip()
            l.debug('command: {}'.format(command))
            if command == "end nominations" and not have_nominations:
                l.info("Command: end nominations")
                new_state['nominations_ended_at'] = pm.created_utc
                have_nominations = True
            if command == "end votes" and not have_votes:
                l.info("Command: end votes")
                new_state['votes_ended_at'] = pm.created_utc
                have_votes = True
            if command == "nominations" and not have_nominations:
                l.info("Command: new nominations thread")
                new_state['nominations_url'] = pm.body
                new_state['nominations_ended_at'] = None
                new_state['counting_nominations'] = True
                have_nominations = True
            if command == "votes" and not have_votes:
                l.info("Command: new votes thread")
                new_state['votes_url'] = pm.body.split()[0]
                new_state['nominated_players'] = pm.body.split()[1:]
                new_state['votes_ended_at'] = None
                new_state['vote_threshold'] = None
                new_state['counting_votes'] = True
                have_votes = True
            if command in ('alive', 'dead', 'gone', 'voteless', 'voteful'):
                player_set = set([x.lower() for x in pm.body.split() if len(x) > 3])
                if command == "alive":
                    l.info("Command: alive players")
                    alive_players.update(player_set)
                elif command == "dead":
                    l.info("Command: dead players")
                    alive_players.difference_update(player_set)
                    dead_players.update(player_set)
                elif command == "gone":
                    l.info("Command: gone players")
                    alive_players.difference_update(player_set)
                    dead_players.difference_update(player_set)
                    voteless_players.difference_update(player_set)
                elif command == "voteless":
                    l.info("Voteless players")
                    voteless_players.update(player_set)
                elif command == "voteful":
                    l.info("Voteful players")
                    voteless_players.difference_update(player_set)
                else:
                    l.warning("Unknown command {}".format(command))
            if command == "max nominations":
                try:
                    self.max_trials = int(pm.body.strip())
                except ValueError:
                    l.warning("Got invalid value for max nominations: {}".format(pm.body.strip()))
            if command == "reset":
                l.warning("Got reset command")
                new_state = Tree()
                alive_players = set()
                dead_players = set()
                voteless_players = set()
            if command == "vote threshold":
                l.info("Command: new vote threshold")
                try:
                    threshold = int(pm.body)
                except ValueError:
                    l.warn("Invalid number given for vote threshold: {}".format(pm.body))
                new_state['vote_threshold'] = threshold

        new_state['alive_players'] = list(alive_players)
        new_state['dead_players'] = list(dead_players)
        new_state['voteless_players'] = list(voteless_players)
        if most_recent_id:
            new_state['most_recent_pm_id'] = most_recent_id
        l.debug("Done processing commands, updating state")
        self.state = new_state

    def get_bot_post(self, submission_url, tag = None):
        l.debug("Fetching submission from {}".format(submission_url))
        submission = praw.objects.Submission.from_url(self.reddit, submission_url)
        l.debug("Got submission")
        comment_to_update = None
        for comment in all_comments(submission.comments):
            if comment.author and comment.author.name == self.bot_username:
                if tag is None:
                    comment_to_update = comment
                    break
                else:
                    if comment.body.lower().find('###{}###'.format(tag.lower())) != -1:
                        comment_to_update = comment
                        break

        if comment_to_update:
            l.debug("Got comment")
        return submission, comment_to_update

    def get_votes(self, vote_post, target_player, old_votes, deadline, get_vote = get_vote_from_post):
        valid_names = {x.lower() for x in self.state['alive_players']}
        #can_vote = valid_names.difference({x.lower() for x in state['voteless_players']})
        can_vote = valid_names
        votes = {}
        for vote_comment in all_comments(vote_post.replies):
            if not vote_comment.author:
                continue
            vote_result = get_vote(vote_comment.body_html)
            if vote_result is None:
                if vote_comment.id not in self.known_invalid_votes:
                    l.warn("Did not get vote result from {}".format(vote_comment.body_html.encode('ascii', errors='ignore')))
                    self.known_invalid_votes.add(vote_comment.id)
                continue

            caster = vote_comment.author.name.lower()
            if caster not in can_vote:
                if vote_comment.id not in self.known_invalid_votes:
                    #voteless is kinda-secret
                    if caster not in valid_names:
                        l.info("{} cannot vote ({} can)!".format(caster, valid_names))
                    self.known_invalid_votes.add(vote_comment.id)
                continue

            #Try to find the time the vote was cast
            timestamp = get_edited_time(vote_comment)

            #If the comment was edited, but the vote wasn't changed, count the time
            #as the time of the original vote
            if caster in old_votes:
                old_vote = old_votes[caster]
                if old_vote["lynch"] == vote_result:
                    timestamp = old_vote["timestamp"]

            if deadline and timestamp > deadline:
                continue

            #if multiple votes are present, count the latest one
            if (caster not in votes) or votes[caster]['timestamp'] > timestamp:
                votes[caster] = {"for" : target_player,
                                 #Confusing terminology: in nomination games,
                                 #'lynch' is a bool. In tradition games, it is a
                                 #string with the same meaning as 'for in nomination
                                 #games. 'for' is None in traditional games
                                 "lynch" : vote_result,
                                 "timestamp": timestamp}

        return votes

    def sort_nominations(self, post_state):
        def votes(nominee):
            vote_info = post_state['current_votes'][nominee].values()
            votes = [v['lynch'] for v in vote_info if v['timestamp'] < deadline]
            yays = sum(votes)
            nays = len(votes) - yays
            return yays, nays
        deadline = post_state['deadline'] if post_state['deadline'] else float('Inf')
        sorted_nominations = post_state['current_nominations'].items()
        sorted_nominations.sort(key = lambda x: (x[0] not in self.state['dead_players'],
                                                 votes(x[0])[1] - votes(x[0])[0],
                                                 x[1]['timestamp']))
        n_trials = 0
        nominations = []
        for nominee, nomination in sorted_nominations:
            yays, nays = votes(nominee)
            up_for_trial = nominee not in self.state['dead_players'] and n_trials < self.max_trials and yays > nays
            if up_for_trial:
                n_trials += 1
            nominations.append(Nomination(player = nominee,
                                          yays = yays,
                                          nays = nays,
                                          up_for_trial = up_for_trial,
                                          vote_post_id = nomination['ack_id'],
                                          timestamp = nomination['timestamp']))

        nominations.sort(key = lambda x: (not bool(x.yays + x.nays), x.timestamp))

        return nominations

    def fix_case(self, username):
        if username in self.state['name_case_cache']:
            return self.state['name_case_cache'][username]

        l.debug("Finding proper name for {}".format(username))
        try:
            user = praw.objects.Redditor(self.reddit, username)
        except requests.HTTPError:
            l.warn("Username {} doesn't appear to exist!".format(username))
            self.state['name_case_cache'][username] = username
            return username
        #there should be a better way...
        try:
            comment = user.get_comments().next()
        except:
            comment = None
        if not comment:
            l.warn("No comments by {}? can't work out their proper name!".format(username))
            self.state['name_case_cache'][username] = username
            return username
        l.debug("{} -> {}".format(username, comment.author.name))
        self.state['name_case_cache'][username] = comment.author.name
        return comment.author.name

    def update_post(self, submission, post, post_template, target = None):
        l.debug("Updating post from template {}".format(post_template))
        if submission:
            with open(post_template) as post_template_fd:
                template = simpletemplate.SimpleTemplate(post_template_fd.read())
                post_contents = template.render(state = self.state, target = target,
                                                sort_nominations = self.sort_nominations,
                                                time = timestamp_to_date,
                                                post = post,
                                                output_url = self.args.output_url,
                                                fix_case = self.fix_case,
                                                args = self.args)

            if not post:
                l.info("Making new post")
                if not args.dry_run:
                    submission.add_comment(post_contents)
                l.info(post_contents)
            else:
                if post.body.strip() != post_contents.strip():
                    l.info("Updating post")
                    if not args.dry_run:
                        post.edit(post_contents)
                    l.info(post_contents)

        l.debug("Done updating post")

    def update_log(self, filename, post, template):
        l.debug("Updating logfile {}".format(filename))
        if args.dry_run:
            return
        with open(template) as template_fd:
            template = simpletemplate.SimpleTemplate(template_fd.read())
            contents = template.render(state = self.state, post = post,
                                       time = timestamp_to_date,
                                       fix_case = self.fix_case,
                                       args = self.args)
        with open(os.path.join(self.args.output_dir, filename), 'w') as log_fd:
            log_fd.write(contents)

    def load_state(self, state_filename):
        try:
            with open(state_filename) as state_fd:
                self.state = json.load(state_fd, object_hook = Tree)
        except IOError:
            pass

        if self.state['game_type'] and self.state['game_type'] != self.args.game_type:
            raise RuntimeError("Wrong game type for state! state is {}, we're running {}".format(state['game_type'], self.args.game_type))

        self.state['game_type'] = self.args.game_type


    def save_state(self, state_filename):
        if not state_filename:
            return
        with open(state_filename, 'w') as state_fd:
            json.dump(self.state, state_fd, indent=2)

class NominationBot(VoteBot):
    def acknowledge_nomination(self, comment, target):
        for potential_bot_comment in all_comments(comment.replies):
            if potential_bot_comment.author.name == self.bot_username:
                #TODO: more checking here
                l.debug("Found old acknowledge post for {}".format(target))
                return potential_bot_comment
        with open('nomination_ack.template') as post_template_fd:
            template = simpletemplate.SimpleTemplate(post_template_fd.read())
            post_contents = template.render(state = self.state, target = target, fix_case = self.fix_case)
        l.info("Acknowledging nomination for {}".format(target))
        if args.dry_run:
            return None
        else:
            return comment.reply(post_contents)

    def get_nominations(self, nomination_post):
        l.debug("Counting nominations")
        new_state = copy.deepcopy(self.state)
        valid_names = {x.lower() for x in self.state['alive_players']}
        #TODO: voteless does not affect nominations currently.
        nomination_state = new_state['nominations'][nomination_post.id]
        nomination_state['deadline'] = new_state['nominations_ended_at']
        nominations = nomination_state['current_nominations']
        for nomination_comment in all_comments(nomination_post.replies):
            nominee = get_nomination_from_post(nomination_comment.body_html, valid_names)
            if not nominee:
                continue
            if not nomination_comment.author:
                continue
            caster = nomination_comment.author.name.lower()
            if caster not in valid_names:
                if nomination_comment not in known_invalid_votes:
                    l.info("{} cannot nominate ({} can)!".format(caster, valid_names))
                    known_invalid_votes.add(nomination_comment.id)
                continue

            if nominee in nominations:
                continue

            #Try to find the time the nomination was made
            timestamp = get_edited_time(nomination_comment)

            if self.state['nominations_ended_at'] and timestamp > self.state['nominations_ended_at']:
                continue

            ack = self.acknowledge_nomination(nomination_comment, nominee)
            vote_history = nomination_state.get('vote_history', [])
            if not vote_history:
                vote_history = []
            vote_history.append({"action": "nominated",
                                 "by": caster,
                                 "on": nominee,
                                 "time": timestamp})
            nomination_state['vote_history'] = vote_history

            nominations[nominee] = {"by" : caster,
                                    "timestamp": timestamp,
                                    "ack_id": ack.id,
                                    "for" : nominee}

        by_acks_id = {}

        for nominee, nomination in nominations.items():
            by_acks_id[nomination['ack_id']] = nomination

        for nomination_comment in all_comments(nomination_post.replies):
            for ack_comment in all_comments(nomination_comment.replies):
                if ack_comment.id in by_acks_id:
                    nomination = by_acks_id[ack_comment.id]
                    nominee = nomination['for']
                    old_votes = copy.deepcopy(nomination_state['current_votes'][nominee])
                    votes = self.get_votes(ack_comment, nominee, old_votes, self.state['nominations_ended_at'])
                    nomination_state['current_votes'][nominee] = votes
                    additions, removals = compare_dicts(old_votes, votes)
                    vote_history = nomination_state.get('vote_history', [])
                    if not vote_history:
                        vote_history = []
                    for voter, vote in additions.items():
                        vote_history.append({"action" : "vote",
                                             "lynch" : vote['lynch'],
                                             "by" : voter,
                                             "for" : vote['for'],
                                             "time" : vote['timestamp']})
                    for voter, vote in removals.items():
                        timestamp = votes[voter]['timestamp'] if voter in votes else int(time.time())
                        vote_history.append({"action" : "unvote",
                                             "lynch" : vote['lynch'],
                                             "by" : voter,
                                             "for" : vote['for'],
                                             "time" : timestamp})
                    nomination_state['vote_history'] = vote_history
                    break

        if new_state['nominations_ended_at']:
            new_state['counting_nominations'] = False
        self.state = new_state
        l.debug("Done counting nominations")

    def update_state(self):
        self.process_commands()
        if self.state['nominations_url'] and self.state['counting_nominations']:
            nomination_submission, nomination_post = self.get_bot_post(self.state['nominations_url'], 'nominate')
            if nomination_post:
                self.get_nominations(nomination_post)
                self.update_log('{}_history.txt'.format(nomination_post.id),
                                nomination_post, 'vote_history.template')
                self.update_log('{}_votes.txt'.format(nomination_post.id),
                                nomination_post, 'nomination_state.template')
            self.update_post(nomination_submission, nomination_post, 'nomination_post.template',
                             target=nomination_post.id if nomination_post else None)

        if self.state['votes_url'] and self.state['counting_votes']:
            for nominee in self.state['nominated_players']:
                votes_submission, votes_post = self.get_bot_post(self.state['votes_url'], 'vote ' + nominee)
                if votes_post:
                    self.count_votes(votes_post, nominee)
                    self.update_log('{}_history.txt'.format(votes_post.id),
                                    votes_post, 'vote_history.template')
                    self.update_log('{}_votes.txt'.format(votes_post.id),
                                    votes_post, 'vote_state.template')
                self.update_post(votes_submission, votes_post, 'vote_post.template', nominee)
            if self.state['votes_ended_at']:
                self.state['counting_votes'] = False

    def count_votes(self, vote_post, nominee):
        l.debug("Counting votes")
        new_state = copy.deepcopy(self.state)
        old_votes = self.state['votes'][vote_post.id]['current_votes']
        votes_state = new_state['votes'][vote_post.id]
        votes = self.get_votes(vote_post, nominee, old_votes, self.state['votes_ended_at'])

        additions, removals = compare_dicts(old_votes, votes)
        vote_history = votes_state.get('vote_history', [])
        if not vote_history:
            vote_history = []
        for voter, vote in additions.items():
            vote_history.append({"action" : "vote",
                                 "lynch" : vote['lynch'],
                                 "by" : voter,
                                 "for" : vote['for'],
                                 "time" : vote['timestamp']})
        for voter, vote in removals.items():
            timestamp = votes[voter]['timestamp'] if voter in votes else int(time.time())
            vote_history.append({"action" : "unvote",
                                 "lynch" : vote['lynch'],
                                 "by" : voter,
                                 "for" : vote['for'],
                                 "time" : timestamp})

        votes_state['vote_history'] = vote_history
        votes_state['current_votes'] = votes

        self.state = new_state
        l.debug("Done counting votes")


class TraditionalBot(VoteBot):
    def update_state(self):
        self.process_commands()
        self.state['name_case_cache']['no lynch'] = 'No Lynch'
        if self.state['votes_url']:
            vote_submission, vote_post = self.get_bot_post(self.state['votes_url'], 'vote')
            if vote_post:
                self.count_votes(vote_post)
                self.update_log('{}_history.txt'.format(vote_post.id),
                                vote_post, 'vote_history_traditional.template')
                self.update_log('{}_votes.txt'.format(vote_post.id),
                                vote_post, 'vote_state_traditional.template')

                votes = self.state['votes'][vote_post.id]['current_votes']
                vote_counts = collections.Counter([v['lynch'] for v in votes.values()])
                real_vote_counts = collections.Counter([v['lynch'] for caster, v in votes.items()
                                   if caster not in self.state['voteless_players']])
                if not self.args.secret_voteless:
                    vote_counts = real_vote_counts
                vote_threshold = self.state['vote_threshold']
                if not isinstance(vote_threshold, int):
                    vote_threshold = (len(self.state['alive_players']) - len(self.state['voteless_players']))/ 2 + 1
                if len(vote_counts) and real_vote_counts.most_common(1)[0][1] >= vote_threshold and not self.state['votes_ended_at'] and args.hammers:
                    self.state['votes_ended_at'] = time.time()
                    v_url = state['votes_url']
                    self.state['votes_url'] = ""
                    lynched_player = real_vote_counts.most_common(1)[0][0]
                    for user in self.authorized_users:
                        if not args.dry_run:
                            self.reddit.send_message(user, "Hammer",
                            "The voting at {} has reached "
                            "a majority for {} . You might want to check the voting "
                            "history and edit times if there were a few last-minute vote changes".format(v_url, lynched_player))
            self.update_post(vote_submission, vote_post, 'vote_post_traditional.template', None)
        self.update_log('players.txt', None, 'players.template')


    def count_votes(self, vote_post):
        l.debug("Counting votes")
        new_state = copy.deepcopy(self.state)

        old_votes = self.state['votes'][vote_post.id]['current_votes']
        votes_state = new_state['votes'][vote_post.id]

        valid_names = {x.lower() for x in self.state['alive_players']}
        valid_names.add('no lynch')

        def get_vote(post_contents):
            res = get_nomination_from_post(post_contents, valid_names)
            return res

        votes = self.get_votes(vote_post, None, old_votes, self.state['votes_ended_at'], get_vote = get_vote)

        additions, removals = compare_dicts(old_votes, votes)
        vote_history = votes_state.get('vote_history', [])
        if not vote_history:
            vote_history = []
        for voter, vote in additions.items():
            vote_history.append({"action" : "vote",
                                 "for" : vote['lynch'],
                                 "by" : voter,
                                 "time" : vote['timestamp']})

        for voter, vote in removals.items():
            timestamp = votes[voter]['timestamp'] if voter in votes else int(time.time())
            vote_history.append({"action" : "unvote",
                                 "for" : vote['lynch'],
                                 "by" : voter,
                                 "time" : timestamp})

        votes_state['vote_history'] = vote_history
        votes_state['current_votes'] = votes

        self.state = new_state
        l.debug("Done counting votes")

def oauth_login(r):
    r.set_oauth_app_info(client_id = creds.oauth_id,
                         client_secret = creds.oauth_secret,
                         redirect_uri="http://127.0.0.1:65010/authorize_callback")
    url = r.get_authorize_url('PloungeMafiaVoteBot', 'identity submit privatemessages edit history read', True)
    print(url)
    key = raw_input('key>').strip()
    access_info = r.get_access_information(key)
    r.set_access_credentials(**access_info)
    return access_info

def oauth_load(r):
    r.set_oauth_app_info(client_id = creds.oauth_id,
                         client_secret = creds.oauth_secret,
                         redirect_uri="http://127.0.0.1:65010/authorize_callback")
    with open('oauth_info.json') as access_fd:
        access_info = json.load(access_fd)
        access_info['scope'] = set(access_info['scope'])
    r.set_access_credentials(**access_info)
    return access_info

def oauth_refresh(r, access_info):
    access_info = r.refresh_access_information(access_info['refresh_token'])
    r.set_access_credentials(**access_info)
    with open('oauth_info.json', 'w') as access_fd:
        json_safe_access_info = dict(access_info)
        json_safe_access_info['scope'] = list(json_safe_access_info['scope'])
        json.dump(json_safe_access_info, access_fd, indent=2)
    return access_info

if __name__ == "__main__":
    args = parser.parse_args()

    l.setLevel(debug_levels[args.log_level])
    l.info("Starting up")
    r = praw.Reddit(user_agent = "VoteCountBot by rcxdude")

    bots = []
    last_refresh_time = None
    oauth_access_info = None


    for game in config.games:
        if game.name.lower() not in [x.lower() for x in config.enabled_games]:
            continue

        BotClass = {
            "nomination" : NominationBot,
            "traditional" : TraditionalBot,
        }[game.game_type]

        bot = BotClass(r, creds, game)
        bot.load_state(bot.args.state_file)
        bot.setup_dir()
        bots.append(bot)

    while True:
        l.info("Attempting login")
        try:
            if args.oauth_login:
                oauth_access_info = oauth_login(r)
            else:
                oauth_access_info = oauth_load(r)
            #r.login(creds.bot_username, creds.bot_password)
            break
        except Exception as e:
            l.error(traceback.format_exc())
            time.sleep(60 * args.update_delay)

    oauth_access_info = oauth_refresh(r, oauth_access_info)
    last_refresh_time = time.time()

    l.info("Logged in")

    while True:
        for bot in bots:
            try:
                bot.update_state()
                bot.save_state(bot.args.state_file)
            except Exception as e:
                l.error(traceback.format_exc())
        if args.oneshot:
            break
        if last_refresh_time - time.time() > 40 * 60:
            l.info("Refreshing OAuth information")
            oauth_access_info = oauth_refresh(r, oauth_access_info)
            last_refresh_time = time.time()
        l.debug("done, sleeping for {} seconds".format(60 * args.update_delay))
        time.sleep(60 * args.update_delay)
