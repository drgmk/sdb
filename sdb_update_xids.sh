#!/bin/sh

# need an argument
if [[ $# -ne 1 ]]
then
echo "give sdbid as argument"
exit
fi

source sdb_insert_config

# and that arg needs to start correctly
if ! [[ $1 =~ ^$sdbprefix ]]
then
echo "id needs to start with $sdbprefix"
fi

# first argument is sdbid
sdbid=$1

tmp=tmp$RANDOM
tmp1=tmp$RANDOM
tmp2=tmp$RANDOM

# get the simbad id, and replace with unicode strings for url
id=$(mysql $db -N -e "SELECT main_id FROM simbad WHERE sdbid = '$sdbid';")
cid=`echo "$id" | sed 's/ /%20/g' | sed 's/+/%2B/g' | sed 's/\*/%2A/g' | sed 's/\[/%5B/g' | sed 's/\]/%5D/g'`

# simbad, put results in temporary table
echo "\nUsing id $id to get xids for $sdbid"

# create table with previous results
#mysql $db -N -e "CREATE TABLE $tmp SELECT sdbid,xid FROM xids WHERE sdbid='$sdbid';"

# xids, remove duplicates that just have different case, sql setup is case-insensitive
csdbid=`echo "$sdbid" | sed 's/+/%2B/g'`
curl -s "http://simbad.u-strasbg.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=votable&query=SELECT%20%27$csdbid%27%20as%20sdbid,id2.id%20as%20xid%20FROM%20ident%20AS%20id1%20JOIN%20ident%20AS%20id2%20USING(oidref)%20WHERE%20id1.id=%27$cid%27;" | $stilts tpipe in=- ifmt=votable cmd='random' cmd='addcol xiduc toUpperCase(xid)' cmd='sort xiduc' cmd='uniq xiduc' cmd='delcols xiduc' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=$tmp write=dropcreate

# remove spaces that appear in IDs
mysql $db -N -e "UPDATE $tmp SET xid = REPLACE(REPLACE(REPLACE(xid,' ','<>'),'><',''),'<>',' ');"

# take relevant rows from xids, find new rows from query above
mysql $db -N -e "CREATE TABLE $tmp1 SELECT sdbid,xid FROM xids WHERE sdbid='$sdbid';"
mysql $db -N -e "CREATE TABLE $tmp2 SELECT $tmp.sdbid,$tmp.xid from $tmp LEFT JOIN $tmp1 USING (xid) WHERE $tmp1.sdbid IS NULL;"

# and insert these into xids
mysql $db -N -e "INSERT INTO xids SELECT DISTINCT sdbid,xid from $tmp2;"

# delete
mysql $db -N -e "DROP TABLE $tmp;"
mysql $db -N -e "DROP TABLE $tmp1;"
mysql $db -N -e "DROP TABLE $tmp2;"
