#!/usr/bin/env python2.7

import collections

Game = collections.namedtuple("Game", 
    ["name",
     "game_type",
     "hammers",
     "secret_voteless",
     "output_dir",
     "output_url",
     "state_file",
     "authorized_users",
     ])

BASE_URL = "http://rcxdu.de/mafia/{}"
BASE_DIR = "/var/www/mafia/{}"

games = [
    Game(name = "example_game",
         game_type = "traditional",
         hammers = True,
         secret_voteless = False,
         output_dir = BASE_DIR.format("example_game"),
         output_url = BASE_URL.format("example_game"),
         state_file = "example.json",
         authorized_users = {"rcxdude"}),
]

enabled_games = {"example_game}
