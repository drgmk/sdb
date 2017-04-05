#!/bin/sh

# insert new photometry without re-importing
db=sdb

mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN apass USING (sdbid) WHERE apass.sdbid IS NULL;" | while read name
do
    echo "\ngetting:"$name
    ./sdb_insert_apass.sh "$name"
done

mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN denis USING (sdbid) WHERE denis.sdbid IS NULL;" | while read name
do
    echo "\ngetting:"$name
    ./sdb_insert_denis.sh "$name"
done

