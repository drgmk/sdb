#!/bin/sh

# insert new photometry without re-importing
db=sdb

# galex
mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN galex USING (sdbid) WHERE galex.sdbid IS NULL;" | while read name
do
echo "\ngetting:"$name
./sdb_insert_galex.sh "$name"
done

# apass
#mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN apass USING (sdbid) WHERE apass.sdbid IS NULL;" | while read name
#do
#    echo "\ngetting:"$name
#    ./sdb_insert_apass.sh "$name"
#done

# tyc2
mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN tyc2 USING (sdbid) WHERE tyc2.sdbid IS NULL;" | while read name
do
    echo "\ngetting:"$name
    ./sdb_insert_tyc2.sh "$name"
done

# gaia
mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN gaia USING (sdbid) WHERE gaia.sdbid IS NULL;" | while read name
do
echo "\ngetting:"$name
./sdb_insert_gaia.sh "$name"
done

# 2mass
mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN 2mass USING (sdbid) WHERE 2mass.sdbid IS NULL;" | while read name
do
    echo "\ngetting:"$name
    ./sdb_insert_2mass.sh "$name"
done

# denis
#mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN denis USING (sdbid) WHERE denis.sdbid IS NULL;" | while read name
#do
#    echo "\ngetting:"$name
#    ./sdb_insert_denis.sh "$name"
#done

# akari irc
#mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN akari_irc USING (sdbid) WHERE akari_irc.sdbid IS NULL;" | while read name
#do
#echo "\ngetting:"$name
#./sdb_insert_akari_irc.sh "$name"
#done

# allwise
#mysql $db -N -e "SELECT sdbid FROM sdb_pm LEFT JOIN allwise USING (sdbid) WHERE allwise.sdbid IS NULL;" | while read name
#do
#    echo "\ngetting:"$name
#    ./sdb_insert_allwise.sh "$name"
#done

# herschel xids
#mysql $db -N -e "SELECT sdbid FROM sdb_pm;" | while read name
#do
#    for t in HPPSC_070_v1 HPPSC_100_v1 HPPSC_160_v1 HPESL_v1 spsc_standard_250_v2 spsc_standard_350_v2 spsc_standard_500_v2;
#    do
#        ./sdb_herschel_psc_xids.sh "$name" "$t"
#    done
#done
