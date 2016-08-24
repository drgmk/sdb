#!/bin/sh

# give a table name in the database to get IDs from

# some other knobs
db=sed_db
user=grant
pass=grant

# given sample db name in $1
mysql -u$user -p$pass $db -N -e "SELECT name FROM $1" | while read name
do
    echo getting:$name
    ./db-insert-one.sh "$name"
done
