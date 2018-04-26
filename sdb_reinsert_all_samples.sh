#!/bin/sh

db=sdb_samples

mysql $db -N -e "SHOW TABLES;" | while read t
do
    nn=`mysql $db -N -e "select count(name) from $t;"`
    ns=`mysql $db -N -e "select count(sdbid) from $t;"`
    if [ $nn != $ns ]
    then
        echo $t
        ./db-insert-many.sh $t
    fi
done
