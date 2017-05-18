#!/bin/sh

# insert new photometry without re-importing
db=sdb

# apass
#mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN apass USING (sdbid) WHERE apass.sdbid IS NULL;" | while read name
#do
#    echo "\ngetting:"$name
#    ./sdb_insert_apass.sh "$name"
#done

#denis
#mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN denis USING (sdbid) WHERE denis.sdbid IS NULL;" | while read name
#do
#    echo "\ngetting:"$name
#    ./sdb_insert_denis.sh "$name"
#done

# herschel xids
mysql $db -N -e "SELECT sdbid FROM sdb_pm;" | while read name
do
    for t in HPPSC_070_v1 HPPSC_100_v1 HPPSC_160_v1 HPESL_v1 spsc_standard_250_v2 spsc_standard_350_v2 spsc_standard_500_v2;
    do
        ./sdb_herschel_psc_xids.sh "$name" "$t"
    done
done
