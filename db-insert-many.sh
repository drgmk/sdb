#!/bin/sh

# give a table name in the database to get IDs from, optional second arg
# is the column name to give to SQL, or if "coord" indicates coords to
# be grabbed from "ra" and "dec" columns

db=sdb_samples
dbroot=/Users/grant/astro/projects/sdb/
logroot=/Users/grant/a-extra/sdb/log/

if [ "$#" -gt 1 ]
then
    name=$2
else
    name='name'
fi

# in case this column doesn't exist
mysql $db -N -e "ALTER TABLE $1 ADD COLUMN sdbid VARCHAR(25);"

# add sdbids to save trying to ingest them
mysql $db -N -e "UPDATE $1 LEFT JOIN sdb.xids ON name=xid SET $1.sdbid=xids.sdbid;"

# given sample db name in $1
if [ "$name" == "coord" ]
then
    mysql $db -N -e "SELECT name FROM $1 WHERE sdbid IS NULL" | while read name
    do
        ra=`mysql $db -N -e "SELECT ra FROM $1 WHERE name='$name'"`
        dec=`mysql $db -N -e "SELECT de FROM $1 WHERE name='$name'"`
        echo getting:"$name ($ra, $dec)"
        "$dbroot"sdb/db-insert-one.sh $ra $dec 2>&1 | tee "$logroot""$name".log
    done
else
    mysql $db -N -e "SELECT name FROM $1 WHERE sdbid IS NULL AND name IS NOT NULL" | while read name
    do
        echo getting:"$name"
        "$dbroot"sdb/db-insert-one.sh "$name" 2>&1 | tee "$logroot""$name".log
    done
fi

proj=`echo $1 | sed "s/.*\.//"`

# modify source table, these bits may need to be done by hand
# TODO: put this in a separate script
echo "executing sql to add/update sdbids"
mysql $db -N -e "UPDATE $1 LEFT JOIN sdb.xids ON name=xid SET $1.sdbid=xids.sdbid;"
mysql $db -N -e "ALTER TABLE $1 ADD INDEX sdbid_$proj (sdbid);"
mysql $db -N -e "ALTER TABLE $1 ADD CONSTRAINT sdbid_$proj FOREIGN KEY (sdbid) REFERENCES sdb.sdb_pm (sdbid);"
mysql $db -N -e "DELETE FROM sdb.projects WHERE project='$proj';"
mysql $db -N -e "INSERT INTO sdb.projects SELECT sdbid,'$proj' FROM $1 WHERE sdbid IS NOT NULL;"
