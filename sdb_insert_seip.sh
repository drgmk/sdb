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

# make a file to join the results to below
fid=/tmp/pos$RANDOM.txt
echo "#sdbid                    nothing" > $fid
#     sdb-v1-183656.34+374701.3 bla  # to get spacing right for ascii table
echo $sdbid $sdbid >> $fid

# SEIP, epoch less certain, roughly 2006.9 for mid-mission
echo "\nLooking for SEIP entry"
res=$(mysql $db -N -e "SELECT sdbid FROM seip WHERE sdbid='$sdbid';")
if [ "$res" == "" ]
then
    epoch=2006.9
    co=$(mysql $db -N -e "SELECT CONCAT(raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600.,',',dej2000 + ($epoch-2000.0) * pmde/1e3/3600.) from sdb_pm where sdbid = '$sdbid';")

    if [ "$co" != "" ]
    then
        echo $co
        ftmp=/tmp/pos$RANDOM.txt
        curl -s "http://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query?catalog=slphotdr4&spatial=cone&radius=$rad&outrows=1&outfmt=3&objstr=$co" > $ftmp
        
        $stilts tjoin nin=2 in1=$fid ifmt1=ascii icmd1='keepcols sdbid' in2=$ftmp ifmt2=votable icmd2='colmeta -name dec_ dec' ocmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=seip write=append
    fi
else
    echo "  $sdbid already present"
fi
