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

ft=/tmp/pos$RANDOM.txt
ft2=/tmp/pos$RANDOM.txt

# get the simbad id, and replace with unicode strings for url
id=$(mysql $db -N -e "SELECT main_id FROM simbad WHERE sdbid = '$sdbid';")
cid=`echo "$id" | sed 's/ /%20/g' | sed 's/+/%2B/g' | sed 's/\*/%2A/g' | sed 's/\[/%5B/g' | sed 's/\]/%5D/g'`

# create temp file with sdbid in it
echo "#sdbid                    xid" > $ft
#     sdb-v1-183656.34+374701.3 bla  # to get spacing right for ascii table
echo $sdbid $sdbid >> $ft

# simbad, put results in temporary table
echo "\nUsing id $id to find simbad info for $sdbid"
curl -s "http://simbad.u-strasbg.fr/simbad/sim-tap/sync?request=doQuery&lang=adql&format=votable&query=SELECT%20basic.main_id,sp_type,sp_bibcode,plx_value,plx_err,plx_bibcode,otype_shortname,otype_longname%20FROM%20basic%20JOIN%20ident%20ON%20ident.oidref%20=%20oid%20JOIN%20otypedef%20on%20basic.otype%20=%20otypedef.otype%20WHERE%20id=%27$cid%27" > $ft2
$stilts tjoin nin=2 in1=$ft ifmt1=ascii icmd1='keepcols sdbid' in2=$ft2 ifmt2=votable ocmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=tmp write=dropcreate

# update simbad table with new results
mysql $db -N -e "UPDATE simbad LEFT JOIN tmp USING (sdbid) SET simbad.sp_type=tmp.sp_type, simbad.sp_bibcode=tmp.sp_bibcode, simbad.plx_value=tmp.plx_value, simbad.plx_err=tmp.plx_err, simbad.plx_bibcode=tmp.plx_bibcode, simbad.type_short=tmp.otype_shortname, simbad.type_long=tmp.otype_longname WHERE simbad.sdbid=tmp.sdbid;"

mysql $db -N -e "DROP TABLE tmp;"
