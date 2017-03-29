#!/bin/sh

# insert new photometry without re-importing
db=sdb

mysql $db -N -e "SELECT sdbid FROM sdb_pm;" | while read name
do
    echo "\ngetting:"$name
    ./sdb_insert_apass.sh "$name"
    ./sdb_insert_denis.sh "$name"
done
