#!/bin/sh

# delete an sdbid from sdb, use this when an import fails for some
# reason (i.e. is left in the "import_failed" table in sdb)

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
for t in 2mass akari_irc allwise apass denis gaia galex projects seip simbad spectra tyc2 xids sdb_pm import_failed;
do
    st="DELETE FROM $t WHERE sdbid = '$1';"
    echo $st
    mysql $db -N -e "$st"
done

# results
mysql $db_res -N -e "SHOW TABLES;" | while read t
do
    st="DELETE FROM $t WHERE id = '$1';"
    echo $st
    mysql $db_res -N -e "$st"
done
