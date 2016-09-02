#!/bin/sh

# give a table name in the database to get IDs from

db=sed_db

# given sample db name in $1
mysql $db -N -e "SELECT name FROM $1" | while read name
do
    echo getting:$name
    ./db-insert-one.sh "$name"
done

# modify source table, these bits may need to be done by hand
echo "may now want to execute some sql commands like this:"
echo "ALTER TABLE $1 ADD COLUMN sdbid VARCHAR(25);"
echo "UPDATE $1 LEFT JOIN xids ON name=xid SET $1.sdbid=xids.sdbid;"
echo "ALTER TABLE $1 ADD FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);"
echo "DELETE FROM projects WHERE project='shardds';"
echo "INSERT INTO projects SELECT sdbid,'$1' FROM $1 WHERE sdbid IS NOT NULL;"
