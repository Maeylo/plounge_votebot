#!/usr/bin/env python2.7

import collections

Game = collections.namedtuple("Game", 
    ["name",
     "name_pretty",
     "game_type",
     "hammers",
     "secret_voteless",
     "output_dir",
     "output_url",
     "state_file",
     "authorized_users",
     ])

BASE_URL = "http://rcxdu.de/mafia/{}"
PUB_DIR = "/var/www/mafia/{}"
BASE_DIR = "priv_out/{}"

PM4_MODS = {"rcxdude", "jibodeah", "ploungemafia", "-48V", "rogerdodger37", "tortillatime", "Balinares"}

games = [
    Game(name = "dw3",
         name_pretty = "dAmnWerewolves 3: Valhalla",
         game_type = "traditional",
         hammers = True,
         secret_voteless = False,
         output_dir = PUB_DIR.format("damnwere3"),
         output_url = BASE_URL.format("damnwere3"),
         state_file = "damnwere3.json",
         authorized_users = {"rcxdude", "jibodeah"}),
]

old_games = [
    Game(name = "example_game",
         name_pretty = "Example Games",
         game_type = "traditional",
         hammers = True,
         secret_voteless = False,
         output_dir = PUB_DIR.format("example_game"),
         output_url = BASE_URL.format("example_game"),
         state_file = "example.json",
         authorized_users = {"rcxdude"}),

    Game(name = "Test1",
         name_pretty = "Example Games",
         game_type = "nomination",
         hammers = False,
         secret_voteless = False,
         output_dir = BASE_DIR.format("test1"),
         output_url = BASE_URL.format("test1"),
         state_file = "test1.json",
         authorized_users = PM4_MODS),

    Game(name = "Test2",
         name_pretty = "Example Games",
         game_type = "nomination",
         hammers = False,
         secret_voteless = False,
         output_dir = BASE_DIR.format("test2"),
         output_url = BASE_URL.format("test2"),
         state_file = "test2.json",
         authorized_users = PM4_MODS),

    Game(name = "Three Fillies",
         name_pretty = "Example Games",
         game_type = "nomination",
         hammers = False,
         secret_voteless = False,
         output_dir = BASE_DIR.format("ThreeFillies"),
         output_url = BASE_URL.format("ThreeFillies"),
         state_file = "threefillies.json",
         authorized_users = PM4_MODS),

    Game(name = "Tulplaza",
         name_pretty = "Example Games",
         game_type = "nomination",
         hammers = False,
         secret_voteless = False,
         output_dir = BASE_DIR.format("Tulplaza"),
         output_url = BASE_URL.format("Tulplaza"),
         state_file = "tulplaza.json",
         authorized_users = PM4_MODS),

    Game(name = "Waiforest",
         name_pretty = "Example Games",
         game_type = "nomination",
         hammers = False,
         secret_voteless = False,
         output_dir = BASE_DIR.format("Waiforest"),
         output_url = BASE_URL.format("Waiforest"),
         state_file = "waiforest.json",
         authorized_users = PM4_MODS),

    Game(name = "Beeswater",
         name_pretty = "Example Games",
         game_type = "nomination",
         hammers = False,
         secret_voteless = False,
         output_dir = BASE_DIR.format("Beeswater"),
         output_url = BASE_URL.format("Beeswater"),
         state_file = "beeswater.json",
         authorized_users = PM4_MODS),

    Game(name = "PM4",
         name_pretty = "Example Games",
         game_type = "nomination",
         hammers = False,
         secret_voteless = False,
         output_dir = PUB_DIR.format("PM4"),
         output_url = BASE_URL.format("PM4"),
         state_file = "pm4.json",
         authorized_users = PM4_MODS),
]

enabled_games = {"dw3"}
