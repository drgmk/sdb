#!/bin/sh

# delete files from sdb sedroot that don't appear in the database

tmp=`cat ~/.sdb.conf|grep '^sedroot'`
seddir=${tmp/*= /}
echo cleaning $seddir

ls $seddir | while read d
do
    n=`mysql sdb -N -e "select count(sdbid) FROM sdb_pm WHERE sdbid='$d';"`
    if [ $n -eq 0 ]
    then
        echo $seddir$d:$n:deleted
        rm -r $seddir$d
    elif [ $n -gt 1 ]
    then
        echo $seddir$d:$n:suspect
    fi
done
