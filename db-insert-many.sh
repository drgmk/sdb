#!/bin/sh

# give a table name in the database to get IDs from, optional second arg is the column
# name to give to SQL

db=sdb_samples
dbroot=/Users/grant/astro/projects/sdb/
logroot=/Users/grant/a-extra/sdb/log/

if [ "$#" -gt 1 ]
then
    name=$2
else
    name='name'
fi

# given sample db name in $1
mysql $db -N -e "SELECT $name FROM $1" | while read name
do
    echo getting:"$name"
    "$dbroot"sdb/db-insert-one.sh "$name" 2>&1 | tee "$logroot""$name".log
done

proj=`echo $1 | sed "s/.*\.//"`

# modify source table, these bits may need to be done by hand
echo "if this is a real sample you may want to execute some sql:"
echo "ALTER TABLE $1 ADD COLUMN sdbid VARCHAR(25);"
echo "UPDATE $1 LEFT JOIN sdb.xids ON name=xid SET $1.sdbid=xids.sdbid;"
echo "ALTER TABLE $1 ADD INDEX sdbid_$proj (sdbid);"
echo "ALTER TABLE $1 ADD CONSTRAINT sdbid_$proj FOREIGN KEY (sdbid) REFERENCES sdb.sdb_pm (sdbid);"
echo "DELETE FROM sdb.projects WHERE project='$proj';"
echo "INSERT INTO sdb.projects SELECT sdbid,'$proj' FROM $1 WHERE sdbid IS NOT NULL;"
