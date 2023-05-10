#!/bin/sh

# need two arguments
if [[ $# -ne 2 ]]
then
    echo "give sdbid and specific Herschel PSC as argument"
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

# first argument is sdbid, second is db table
sdbid=$1
ctlg=$2

# catalogue details, HPESL has multiple colours, so keep all matches
find=best
case $ctlg in
    "HPPSC_070_v1") pre=HPPSC070A_ ;;
    "HPPSC_100_v1") pre=HPPSC100A_ ;;
    "HPPSC_160_v1") pre=HPPSC160A_ ;;
    "HPESL_v1")     pre=HPESL
                    find=all ;;

    "spsc_standard_250_v2") pre=HSPSC250A_ ;;
    "spsc_standard_350_v2") pre=HSPSC350A_ ;;
    "spsc_standard_500_v2") pre=HSPSC500A_ ;;

    *) echo "don't know catalgue $ctlg"
       exit
       ;;
esac

# see if we have the id already
echo $sdbid
res=$(mysql $db -N -e "SELECT xid FROM xids WHERE sdbid='$sdbid' and xid REGEXP('^$pre');")
if [[ $res == "" ]]
then
    echo "\n$pre ID not present, looking"

    # June 2009 to April 2013, mid is May 2011
    epoch=2011.5

    # coords for cutout
    ra=$(mysql $db -N -e "SELECT raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600.0 from sdb_pm where sdbid = '$sdbid';")
    de=$(mysql $db -N -e "SELECT dej2000 + ($epoch-2000.0) * pmde/1e3/3600. from sdb_pm where sdbid = '$sdbid';")
    echo "  coords $ra,$de"

    # 1 arcmin deg cutout, first check if it's empty
    box=0.017
    nin=$(mysql photometry -N -e "SELECT name from $ctlg where ra between $ra-$box and $ra+$box and dec_ between $de-$box and $de+$box")
    if [[ $nin == "" ]]
    then
        echo "  No objects in $ctlg within 1 arcmin"
    else
        $stilts sqlclient db='jdbc:mysql://localhost/photometry'$ssl user=$user password=$password sql="SELECT * from $ctlg where ra between $ra-$box and $ra+$box and dec_ between $de-$box and $de+$box" ofmt=votable > $ft

        # table to match against
        $stilts sqlclient db='jdbc:mysql://localhost/'$sdb user=$user password=$password sql="SELECT sdbid,raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600. as ra_ep2011,dej2000 + ($epoch-2000.0) * pmde/1e3/3600. as de_ep2011 from sdb_pm where sdbid = '$sdbid'" ofmt=votable > $ft2

        # the match, allow some extra uncertainty in star position since Herschel
        # resolution not very good
        $stilts tmatch2 in1=$ft2 ifmt1=votable in2=$ft ifmt2=votable ocmd='keepcols "sdbid name"' matcher=skyellipse values1='ra_ep2011 de_ep2011 3.0 3.0 0.0' values2='ra dec_ raerr decerr 270.0' params='20' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=xids find=$find write=append
    fi
else
    echo "\nHave $ctlg ID:$res"
fi
