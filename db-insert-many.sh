#!/bin/sh

# give a table name in the database to get IDs from

# some other knobs
db=sed_db
tmp=`cat /etc/my.cnf | grep user | sed 's/ //g'`
eval $tmp
tmp=`cat /etc/my.cnf | grep password | sed 's/ //g'`
eval $tmp

# given sample db name in $1
mysql -u$user -p$password $db -N -e "SELECT name FROM $1" | while read name
do
    echo getting:$name
    ./db-insert-one.sh "$name"
done

# modify source table
mysql -u$user -p$password $db -N -e "ALTER TABLE $1 ADD COLUMN sdbid BIGINT(19);"
mysql -u$user -p$password $db -N -e "UPDATE $1 LEFT JOIN xids ON name=xid SET $1.sdbid=xids.sdbid;"
mysql -u$user -p$password $db -N -e "ALTER TABLE $1 ADD FOREIGN KEY (sdbid) REFERENCES sdb_pm (sdbid);"
mysql -u$user -p$password $db -N -e "DELETE FROM projects WHERE project='shardds';"
mysql -u$user -p$password $db -N -e "INSERT INTO projects SELECT sdbid,'$1' FROM $1 WHERE sdbid IS NOT NULL;"
