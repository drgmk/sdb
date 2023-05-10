#!/usr/bin/env python3

import time
import mysql.connector
import config as cfg

import dd.web

# set up connection
cnx = mysql.connector.connect(user=cfg.mysql['user'],
                              password=cfg.mysql['passwd'],
                              host=cfg.mysql['host'],
                              database='sdb_samples',
                              auth_plugin='mysql_native_password')
cursor = cnx.cursor(buffered=True)

# get coordinates from specified table
table = 'carmenes_m'
namecol = 'name_'
newidcol = '`name`'
racol = 'raj2000'
decol = 'dej2000'

cursor.execute(f"SELECT {namecol},{racol},{decol},{newidcol} FROM {table};")
out = cursor.fetchall()

for row in out:

    if row[3] is not None:
        continue

    time.sleep(0.1)

    id = dd.web.get_simbad_main_id(ra=row[1],dec=row[2])
    if id is None:
        id = dd.web.get_simbad_main_id(name=row[0])

    print(row)
    if id is not None:
        print(id)
        s = f'UPDATE {table} SET {newidcol}="{id}" WHERE {racol}={row[1]} AND {decol}={row[2]};'
        cursor.execute(s)

    cnx.commit()

cursor.close()
cnx.close()
