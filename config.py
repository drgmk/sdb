#!/usr/bin/env python

"""Config for sdb code

This is mostly configured with a .conf file, but some post-processing is
done here. The imformation contained herein can be accessed from 
elsewhere by importing this module:

>>> import config as cfg
>>> root = cfg.file['root']

For now code assumes it will be run in this directory. Shell scripts can
access these parameters with commands like

>ddir=`echo "import config; print(config.mysql['host'])" | python`

"""

import os
import configparser

# info from the config files, defaults are in same dir as this file
cfg = configparser.ConfigParser()
cfg.read([
          os.path.dirname(os.path.realpath(__file__))+'/sdb.conf',
          os.path.expanduser('~')+'/.sdb.conf'
         ])

file = cfg['file']
www = cfg['www']

db = cfg['db']
if db['type'] == 'mysql':
    db.update(cfg['mysql'])
elif db['type'] == 'sqlite':
    db.update(cfg['sqlite'])

phot = {}
phot['merge_dupes'] = cfg['phot']['merge_dupes'].split(',')

# spectra. tuple has location, which is relative to somewhere which is
# given by the file configuration, and two parts of a file glob where
# the aor goes in between
spectra = {'irsstare': ('cassis/irsstare/',
                        'cassis_yaaar_*_', '*.fits'),
           'visir': ('visir/', '*', '*.csv'),
           'spex': ('irtf_spex/', '*', '.fits'),
           'csv': ('csv/', '*', '*')
}
