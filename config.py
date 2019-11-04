# Copyright 2019 Michael Still

import copy
import os


CONFIG_DEFAULTS = {
    'INSTANCE_PATH': '/srv/shaken',     # Where on disk to store instance data
    'ZONE': 'shaken',                   # What nova called an availability zone
}


class Config(object):
    def __init__(self):
        self.config = copy.copy(CONFIG_DEFAULTS)
        for var in os.environ:
            if var.startswith('SHAKEN_'):
                self.config[var.replace('SHAKEN_', '')] = os.environ[var]

    def get(self, var):
        return self.config.get(var)


parsed = Config()
