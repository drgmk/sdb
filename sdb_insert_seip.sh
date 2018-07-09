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
    ra=$(mysql $db -N -e "SELECT raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600. from sdb_pm where sdbid = '$sdbid';")
    de=$(mysql $db -N -e "SELECT dej2000 + ($epoch-2000.0) * pmde/1e3/3600. from sdb_pm where sdbid = '$sdbid';")

    if [ "$ra" != "" ]
    then
        echo $ra,$de
        ftmp=/tmp/pos$RANDOM.txt

        # TAP query, assume first row is closest
        curl -s "https://irsa.ipac.caltech.edu/TAP/sync?QUERY=SELECT+*+FROM+slphotdr4+WHERE+CONTAINS(POINT(%27J2000%27,ra,dec),CIRCLE(%27J2000%27,$ra,$de,0.000555))=1" > $ftmp

#        curl -s "http://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query?catalog=slphotdr4&spatial=cone&radius=$rad&outrows=1&outfmt=3&objstr=$co" > $ftmp

        numrow=`$stilts tpipe in=$ftmp cmd='keepcols objid' cmd='stats NGood' ofmt=csv-noheader`
        echo "N rows $numrow"
        if [[ "$numrow" == "0" ]]
        then
            echo "Nothing found, writing empty entry in $db.seip"
            $(mysql $db -N -e "INSERT INTO $db.seip (sdbid) VALUES ('$sdbid');")
        else

            # check if there's an entry for this source already
            objid=`$stilts tpipe in=$ftmp ifmt=votable cmd=random cmd='keepcols objid' omode=out ofmt=csv-noheader`
            res=$(mysql $db -N -e "SELECT sdbid FROM seip WHERE objid='$objid';")
            if [ "$res" != "" ]
            then
                echo "Removing previous entry for $objid ($res)"
                mysql $db -N -e "DELETE FROM seip WHERE sdbid = '$res';"
            fi

            $stilts tjoin nin=2 in1=$fid ifmt1=ascii icmd1='keepcols sdbid' in2=$ftmp ifmt2=votable icmd2='colmeta -name dec_ dec' ocmd='random' ocmd='rowrange 1 1' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=seip write=append
        fi
    fi
else
    echo "  $sdbid already present"
fi
