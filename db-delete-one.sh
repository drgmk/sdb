#!/bin/sh

# delete an sdbid from sdb

# check we were given an sdbid
if [[ $1 =~ ^sdb-v[0-9]-[0-9]{6}\.[0-9]{2}[+-][0-9]{6}\.[0-9] ]]
then
    echo "$1 ok"
else
    echo "$1 not an sdbid"
    exit
fi

db=sdb
db_samp=sdb_samples
db_res=sdb_results

# samples
mysql $db_samp -N -e "SHOW TABLES;" | while read t
do
    st="UPDATE $t SET sdbid = NULL WHERE sdbid = '$1';"
    echo $st
    mysql $db_samp -N -e "$st"
done

# sbd tables
for t in 2mass akari_irc allwise gaia galex projects sdb_import_finished seip simbad spectra tyc2 xids sdb_pm;
do
    st="DELETE FROM $t WHERE sdbid = '$1';"
    echo $st
    mysql $db -N -e "$st"
done

# results
mysql $db_res -N -e "SHOW TABLES;" | while read t
do
    st="UPDATE $t SET sdbid = NULL WHERE id = '$1';"
    echo $st
    mysql $db_samp -N -e "$st"
done
