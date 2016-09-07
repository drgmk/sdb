#!/bin/sh

# give a table name in the database to get IDs from

db=sed_db
dbroot=/Users/grant/astro/projects/sed-db/

# given sample db name in $1
mysql $db -N -e "SELECT name FROM $1" | while read name
do
    echo getting:"$name"
    "$dbroot"sed-db/db-insert-one.sh "$name" 2>&1 | tee "$dbroot"logs/"$name".log
done

proj=`echo $1 | sed "s/.*\.//"`

# modify source table, these bits may need to be done by hand
echo "may now want to execute some sql commands like this:"
echo "ALTER TABLE $1 ADD COLUMN sdbid VARCHAR(25);"
echo "UPDATE $1 LEFT JOIN sed_db.xids ON name=xid SET $1.sdbid=xids.sdbid;"
echo "ALTER TABLE $1 ADD PRIMARY KEY (`name`);"
echo "ALTER TABLE $1 ADD CONSTRAINT `name` FOREIGN KEY (sdbid) REFERENCES sed_db.sdb_pm (sdbid);"
echo "DELETE FROM sed_db.projects WHERE project='shardds';"
echo "INSERT INTO sed_db.projects SELECT sdbid,'$proj' FROM $1 WHERE sdbid IS NOT NULL;"
