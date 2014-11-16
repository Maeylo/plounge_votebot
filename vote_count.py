#!/usr/bin/env python2.7

import re
import praw
import json
import copy
import time
import pytz
import creds
import os.path
import logging
import argparse
import datetime
import traceback
import prettylog
import collections
import praw.objects
import simpletemplate

l = prettylog.ColoredLogger(__name__)

debug_levels = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG
}

parser = argparse.ArgumentParser(description="Plounge mafia vote counting bot")
parser.add_argument("--state", help="State file to use")
parser.add_argument("--log_level", help="Log level to use", choices =
                     debug_levels.keys(), default = 'info')
parser.add_argument("--output-dir", help="Directory to output state information to")
parser.add_argument("--output-url", help="Base URL at which the output folder can be found")
parser.add_argument("--oneshot", action='store_true', help="run state transition once")
parser.add_argument("--update_delay", type=int, default=5, help="time in minutes between state updates")
parser.add_argument("--game_type", choices = ["nomination", "traditional"], default = 'traditional')

bot_username = creds.bot_username
bot_password = creds.bot_password 

authorized_users = creds.authorized_users

Vote = collections.namedtuple("Vote", ["by", "target", "time"])

def Tree(dict_ = {}):
    tree = collections.defaultdict(Tree)
    tree.update(dict_)
    return tree 

state = Tree()

def process_commands():
    l.debug("Processing commands")
    global state
    new_state = copy.deepcopy(state)
    pms = r.get_inbox()
    have_nominations = False
    have_votes = False
    most_recent_id = None
    alive_players = set(new_state["alive_players"])
    dead_players = set(new_state["dead_players"])
    voteless_players = set(new_state["voteless_players"])
    voteless_players.difference_update(dead_players)
    alive_players.difference_update(dead_players)
    for pm in pms:
        if pm.id == state['most_recent_pm_id']:
            break
        if not most_recent_id:
            most_recent_id = pm.id
        if pm.author.name.lower() not in authorized_users:
            continue
        command = pm.subject.lower().strip()
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
            have_nominations = True
        if command == "votes" and not have_votes:
            l.info("Command: new votes thread")
            new_state['votes_url'] = pm.body.split()[0]
            new_state['nominated_players'] = pm.body.split()[1:]
            new_state['votes_ended_at'] = None
            new_state['vote_threshold'] = None
            have_votes = True
        if command in ('alive', 'dead', 'gone', 'voteless', 'voteful'):
            player_set = set([x.lower() for x in pm.body.split() if len(x) > 3])
            if pm.subject == "alive":
                l.info("Command: alive players")
                alive_players.update(player_set)
            if pm.subject == "dead":
                l.info("Command dead players")
                alive_players.difference_update(player_set)
                dead_players.update(player_set)
            if pm.subject == "gone":
                l.info("Gone players")
                alive_players.difference_update(player_set)
                dead_players.difference_update(player_set)
                voteless_players.difference_update(player_set)
            if pm.subject == "voteless":
                l.info("Voteless players")
                voteless_players.update(player_set)
            if pm.subject == "voteful":
                l.info("Voteful players")
                voteless_players.difference_update(player_set)
        if command == "reset":
            l.warning("Got reset command")
            new_state = Tree()
            alive_players = set()
            dead_players = set()
            break
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
    state = new_state

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

def get_bot_post(submission_url, tag = None):
    l.debug("Fetching submission from {}".format(submission_url))
    submission = praw.objects.Submission.from_url(r, submission_url)
    l.debug("Got submission")
    comment_to_update = None
    for comment in all_comments(submission.comments):
        if comment.author and comment.author.name == bot_username:
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

nominate_re = re.compile("""
(
    (?P<strikethrough>(~~)?)               #stricken through votes don't count
      [^*~]*
        (\*\*|__)                          #must be bold
            [^*~]*?
            (?P<strikethrough_inner>(~~)?)
            [^*~]*?                        #Can preface with whatever
            (nominate|vote|lynch)?         #vote or nominate is for clarity only, they have the same effect
            \s*:?\s*                       #could be a colon or not
            (/u/)?                         #might start with /u/
            (?P<user>no\s*lynch|[^.*~\s]+) #username may consist of any characters except whitespace and *
                                           #'no lynch' is valid in traditional games, and contains a space
            [^*~]*?                        #Can end with whatever
            (?P=strikethrough_inner)
            [^*~]*?
        (\*\*|__)
      [^*~]*
    (?P=strikethrough)
) | (~~[^~]*~~)                            #must match other struck out blocks so that spurious matches don't occur
""", re.VERBOSE)

vote_re = re.compile("""
(
    (?P<strikethrough>(~~)?)      #stricken through votes don't count
      [^*~]*
        (\*\*|__)                 #must be bold
        \s*
        (?P<strikethrough_inner>(~~)?)
        \s*
        (vote)?:?                 #can start with vote or not
        \s*
        (?P<vote>
         yay|lynch|yes|           #many yes or no options. Must be synced with
         nay|pardon|no)           # get_vote_from_post()
        \s*
        (?P=strikethrough_inner)
        \s*
        (\*\*|__)
      [^*~]*
    (?P=strikethrough)
) | (~~[^~]*~~)                            #must match other struck out blocks so that spurious matches don't occur
""", re.VERBOSE)

def get_nomination_from_post(post_contents, valid_names):
    matches = nominate_re.finditer(post_contents.lower())
    valid_votes = []
    for match in matches:
        if match.group('strikethrough'):
            continue
        if not match.group('user'):
            continue
        username = match.group('user').strip().lower()
        if username in valid_names:
            valid_votes.append(username)
    if valid_votes:
        return valid_votes[-1]

def get_vote_from_post(post_contents):
    matches = vote_re.finditer(post_contents.lower())
    valid_votes = []
    for match in matches:
        if match.group('strikethrough') or match.group('strikethrough_inner'):
            continue
        if not match.group('vote'):
            continue
        vote = match.group('vote').strip().lower()
        if vote in ('yay', 'lynch', 'yes'):
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

known_invalid_votes = set()

def get_votes(vote_post, target_player, old_votes, deadline, get_vote = get_vote_from_post):
    valid_names = {x.lower() for x in state['alive_players']}
    #can_vote = valid_names.difference({x.lower() for x in state['voteless_players']})
    can_vote = valid_names
    votes = {}
    for vote_comment in all_comments(vote_post.replies):
        if not vote_comment.author:
            continue
        vote_result = get_vote(vote_comment.body)
        if vote_result is None:
            if vote_comment.id not in known_invalid_votes:
                l.warn("Did not get vote result from {}".format(vote_comment.body.encode('ascii', errors='ignore')))
                known_invalid_votes.add(vote_comment.id)
            continue

        caster = vote_comment.author.name.lower()
        if caster not in can_vote:
            if vote_comment.id not in known_invalid_votes:
                #voteless is kinda-secret
                if caster not in valid_names:
                    l.info("{} cannot vote ({} can)!".format(caster, valid_names))
                known_invalid_votes.add(vote_comment.id)
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

def acknowledge_nomination(comment, target):
    for potential_bot_comment in all_comments(comment.replies):
        if potential_bot_comment.author.name == bot_username:
            #TODO: more checking here
            l.debug("Found old acknowledge post for {}".format(target))
            return potential_bot_comment
    with open('nomination_ack.template') as post_template_fd:
        template = simpletemplate.SimpleTemplate(post_template_fd.read())
        post_contents = template.render(state = state, target = target, fix_case = fix_case)
    l.info("Acknowledging nomination for {}".format(target))
    return comment.reply(post_contents)

def get_nominations(nomination_post):
    global state
    l.debug("Counting nominations")
    new_state = copy.deepcopy(state)
    valid_names = {x.lower() for x in state['alive_players']}
    #TODO: voteless does not affect nominations currently.
    nomination_state = new_state['nominations'][nomination_post.id]
    nomination_state['deadline'] = new_state['nominations_ended_at']
    nominations = nomination_state['current_nominations']
    for nomination_comment in all_comments(nomination_post.replies):
        nominee = get_nomination_from_post(nomination_comment.body, valid_names)
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

        if state['nominations_ended_at'] and timestamp > state['nominations_ended_at']:
            continue

        ack = acknowledge_nomination(nomination_comment, nominee)
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
                votes = get_votes(ack_comment, nominee, old_votes, state['nominations_ended_at'])
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

    state = new_state
    l.debug("Done counting nominations")

Nomination = collections.namedtuple('Nomination', ['player', 'yays', 'nays', 'up_for_trial', 'vote_post_id', 'timestamp'])

def sort_nominations(post_state): 
    def votes(nominee):
        vote_info = post_state['current_votes'][nominee].values()
        votes = [v['lynch'] for v in vote_info if v['timestamp'] < deadline]
        yays = sum(votes)
        nays = len(votes) - yays
        return yays, nays
    deadline = post_state['deadline'] if post_state['deadline'] else float('Inf')
    sorted_nominations = post_state['current_nominations'].items()
    sorted_nominations.sort(key = lambda x: (x[0] not in state['dead_players'],
                                             votes(x[0])[1] - votes(x[0])[0],
                                             x[1]['timestamp']))
    n_trials = 0
    nominations = []
    for nominee, nomination in sorted_nominations:
        yays, nays = votes(nominee)
        up_for_trial = nominee not in state['dead_players'] and n_trials < 5 and yays > nays
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

def count_votes(vote_post, nominee):
    global state
    l.debug("Counting votes")
    new_state = copy.deepcopy(state)
    old_votes = state['votes'][vote_post.id]['current_votes']
    votes_state = new_state['votes'][vote_post.id]
    votes = get_votes(vote_post, nominee, old_votes, state['votes_ended_at'])

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

    state = new_state
    l.debug("Done counting votes")

def count_votes_traditional(vote_post):
    global state
    l.debug("Counting votes")
    new_state = copy.deepcopy(state)

    old_votes = state['votes'][vote_post.id]['current_votes']
    votes_state = new_state['votes'][vote_post.id]

    valid_names = {x.lower() for x in state['alive_players']}
    valid_names.add('no lynch')

    def get_vote(post_contents):
        res = get_nomination_from_post(post_contents, valid_names)
        return res

    votes = get_votes(vote_post, None, old_votes, state['votes_ended_at'], get_vote = get_vote)

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

    state = new_state
    l.debug("Done counting votes")

def timestamp_to_date(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, pytz.utc).isoformat() 

def fix_case(username):
    if username in state['name_case_cache']:
        return state['name_case_cache'][username]

    l.debug("Finding proper name for {}".format(username))
    try:
        user = praw.objects.Redditor(r, username)
    except HTTPError:
        l.warn("Username {} doesn't appear to exist!".format(username))
        state['name_case_cache'][username] = username
        return username
    #there should be a better way...
    comment = user.get_comments().next()
    if not comment:
        l.warn("No comments by {}? can't work out their proper name!".format(username))
        state['name_case_cache'][username] = username
        return username
    l.debug("{} -> {}".format(username, comment.author.name))
    state['name_case_cache'][username] = comment.author.name
    return comment.author.name

def update_post(submission, post, post_template, target = None):
    l.debug("Updating post from template {}".format(post_template))
    if submission:
        with open(post_template) as post_template_fd:
            template = simpletemplate.SimpleTemplate(post_template_fd.read())
            post_contents = template.render(state = state, target = target,
                                            sort_nominations = sort_nominations,
                                            time = timestamp_to_date,
                                            post = post,
                                            output_url = args.output_url,
                                            fix_case = fix_case)

        if not post:
            l.info("Making new post")
            submission.add_comment(post_contents)
            l.info(post_contents)
        else:
            if post.body.strip() != post_contents.strip():
                l.info("Updating post")
                post.edit(post_contents)
                l.info(post_contents)

    l.debug("Done updating post")

def update_log(filename, post, template):
    l.debug("Updating logfile {}".format(filename))
    with open(template) as template_fd:
        template = simpletemplate.SimpleTemplate(template_fd.read())
        contents = template.render(state = state, post = post,
                                   time = timestamp_to_date,
                                   fix_case = fix_case)
    with open(os.path.join(args.output_dir, filename), 'w') as log_fd:
        log_fd.write(contents)

def update_state_nomination():
    process_commands()
    if state['nominations_url']:
        nomination_submission, nomination_post = get_bot_post(state['nominations_url'], 'nominate')
        if nomination_post:
            get_nominations(nomination_post)
            update_log('{}_history.txt'.format(nomination_post.id),
                       nomination_post, 'vote_history.template')
            update_log('{}_votes.txt'.format(nomination_post.id),
                       nomination_post, 'nomination_state.template')
        update_post(nomination_submission, nomination_post, 'nomination_post.template',
                    target=nomination_post.id if nomination_post else None)
    if state['votes_url']:
        for nominee in state['nominated_players']:
            votes_submission, votes_post = get_bot_post(state['votes_url'], 'vote ' + nominee)
            if votes_post:
                count_votes(votes_post, nominee)
                update_log('{}_history.txt'.format(votes_post.id),
                           votes_post, 'vote_history.template')
                update_log('{}_votes.txt'.format(votes_post.id),
                           votes_post, 'vote_state.template')
            update_post(votes_submission, votes_post, 'vote_post.template', nominee)

def update_state_traditional():
    process_commands()
    state['name_case_cache']['no lynch'] = 'No Lynch'
    if state['votes_url']:
        vote_submission, vote_post = get_bot_post(state['votes_url'], 'vote')
        if vote_post:
            count_votes_traditional(vote_post)
            update_log('{}_history.txt'.format(vote_post.id),
                       vote_post, 'vote_history_traditional.template')
            update_log('{}_votes.txt'.format(vote_post.id),
                       vote_post, 'vote_state_traditional.template')

            votes = state['votes'][vote_post.id]['current_votes']
            vote_counts = collections.Counter([v['lynch'] for v in votes.values()])
            real_vote_counts = collections.Counter([v['lynch'] for caster, v in votes.items()
                               if caster not in state['voteless_players']])
            vote_threshold = state['vote_threshold']
            if not isinstance(vote_threshold, int):
                vote_threshold = (len(state['alive_players']) - len(state['voteless_players']))/ 2 + 1
            if len(vote_counts) and real_vote_counts.most_common(1)[0][1] >= vote_threshold and not state['votes_ended_at']:
                state['votes_ended_at'] = time.time()
                v_url = state['votes_url']
                state['votes_url'] = ""
                lynched_player = real_vote_counts.most_common(1)[0][0]
                for user in authorized_users:
                    r.send_message(user, "Hammer", "The voting at {} has reached "
                            "a majority for {} . You might want to check the voting "
                            "history and edit times if there were a few last-minute vote changes".format(v_url, lynched_player))
        update_post(vote_submission, vote_post, 'vote_post_traditional.template', None)
    update_log('players.txt', None, 'players.template')

def load_state(state_filename):
    global state
    try:
        with open(state_filename) as state_fd:
            state = json.load(state_fd, object_hook = Tree)
    except IOError:
        pass

def save_state(state_filename):
    if not state_filename:
        return
    with open(state_filename, 'w') as state_fd:
        json.dump(state, state_fd, indent=2)

if __name__ == "__main__":
    args = parser.parse_args()

    update_state = {
        "nomination" : update_state_nomination,
        "traditional" : update_state_traditional,
    }[args.game_type]

    l.setLevel(debug_levels[args.log_level])
    l.info("Starting up")
    r = praw.Reddit(user_agent = "VoteCountBot by rcxdude")

    load_state(args.state)

    if state['game_type'] and state['game_type'] != args.game_type:
        raise RuntimeError("Wrong game type for state! state is {}, we're running {}".format(state['game_type'], args.game_type))

    state['game_type'] = args.game_type

    while True:
        l.info("Attempting login")
        try:
            r.login(bot_username, bot_password)
            break
        except Exception as e:
            l.error(traceback.format_exc())
            time.sleep(60 * args.update_delay)

    l.info("Logged in")

    while True:
        try:
            update_state()
            save_state(args.state)
        except Exception as e:
            l.error(traceback.format_exc())
        if args.oneshot:
            break
        time.sleep(60 * args.update_delay)
