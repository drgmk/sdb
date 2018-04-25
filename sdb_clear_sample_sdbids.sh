#!/bin/sh

# clear sdbids from sample tables

db_samp=sdb_samples

mysql $db_samp -N -e "SHOW TABLES;" | while read t
do
    st="UPDATE $t SET sdbid = NULL;"
    echo $st
    mysql $db_samp -N -e "$st"
done
