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

ft=/tmp/pos$RANDOM.txt
ft2=/tmp/pos$RANDOM.txt

# first argument is sdbid
sdbid=$1

# IRAS FSC, this catalogue has FK5 positions at epoch 1983.5. assume
# IRAS ellipse uncertainty much larger than search position. grab subset
# of IRAS within 10deg first
res=$(mysql $db -N -e "SELECT xid FROM xids WHERE sdbid='$sdbid' and xid REGEXP('^IRAS F');")
if [[ $res == "" ]]
then
    echo "\nIRAS FSC ID not present, looking"
    $stilts sqlclient db='jdbc:mysql://localhost/photometry'$ssl user=$user password=$password sql="SELECT * from iras_fsc where _raj2000 between $ra-5.0 and $ra+5.0 and _dej2000 between $de-5.0 and $de+5.0" ofmt=votable > $ft
    $stilts sqlclient db='jdbc:mysql://localhost/'$sdb user=$user password=$password sql="SELECT sdbid,raj2000 + (1983.5-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600. as ra_ep1983p5,dej2000 + (1983.5-2000.0) * pmde/1e3/3600. as de_ep1983p5 from sdb_pm where sdbid = '$sdbid'" ofmt=votable > $ft2
    $stilts tmatch2 in1=$ft2 ifmt1=votable in2=$ft ifmt2=votable ocmd='keepcols "sdbid IRAS_ID"' matcher=skyellipse values1='ra_ep1983p5 de_ep1983p5 1.0 1.0 0.0' values2='_RAJ2000 _DEJ2000 Major Minor PosAng' params='20' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=xids find=best write=append
else
    echo "\nHave IRAS FSC ID:$res"
fi

# IRAS PSC, as above
res=$(mysql $db -N -e "SELECT xid FROM xids WHERE sdbid='$sdbid' and xid REGEXP('^IRAS [0-9]');")
if [[ $res == "" ]]
then
    echo "\nIRAS PSC ID not present, looking"
    $stilts sqlclient db='jdbc:mysql://localhost/photometry'$ssl user=$user password=$password sql="SELECT * from iras_psc where _raj2000 between $ra-5.0 and $ra+5.0 and _dej2000 between $de-5.0 and $de+5.0" ofmt=votable > $ft
    $stilts sqlclient db='jdbc:mysql://localhost/'$sdb user=$user password=$password sql="SELECT sdbid,raj2000 + (1983.5-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600. as ra_ep1983p5,dej2000 + (1983.5-2000.0) * pmde/1e3/3600. as de_ep1983p5 from sdb_pm where sdbid = '$sdbid'" ofmt=votable > $ft2
    $stilts tmatch2 in1=$ft2 ifmt1=votable in2=$ft ifmt2=votable ocmd='keepcols "sdbid IRAS_ID"' matcher=skyellipse values1='ra_ep1983p5 de_ep1983p5 1.0 1.0 0.0' values2='_RAJ2000 _DEJ2000 Major Minor PosAng' params='20' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=xids find=best write=append
else
    echo "\nHave IRAS PSC ID:$res"
fi
