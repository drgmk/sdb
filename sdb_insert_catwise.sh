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

# catWISE, assume 2000.0 as stated
echo "\nLooking for catWISE entry"
res=$(mysql $db -N -e "SELECT sdbid FROM catwise WHERE sdbid='$sdbid';")
if [ "$res" == "" ]
then
    epoch=2000.0
    co=$(mysql $db -N -e "SELECT CONCAT(raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600.,',',dej2000 + ($epoch-2000.0) * pmde/1e3/3600.) from sdb_pm where sdbid = '$sdbid';")

    if [ "$co" != "" ]
    then
        echo $co
        fviz=/tmp/pos$RANDOM.txt
        ftmp=/tmp/pos$RANDOM.txt
        vizquery -site=$site -mime=votable -source=II/365/catwise -c.rs=$rad -sort=_r -out.max=1 -out.add=_r -out.add=e_W1mproPM -out.add=e_W2mproPM -out.add=chi2W1pm -out.add=chi2W2pm -out.add=dW1mpro -out.add=dW2mpro -c="$co" > $fviz
        tout=`$stilts tjoin nin=2 in1=$fid ifmt1=ascii icmd1='keepcols sdbid' in2=$fviz ifmt2=votable ocmd='random' omode=out ofmt=votable 2>&1 > $ftmp`

        if [[ "$tout" == "Error: No TABLE element found" ]]
        then
            echo "Nothing found, writing empty entry in $db.catwise"
            $(mysql $db -N -e "INSERT INTO $db.catwise (sdbid) VALUES ('$sdbid');")
        else
            # check if there's an entry for this source already
            name=`$stilts tpipe in=$ftmp ifmt=votable cmd=random cmd='keepcols name' omode=out ofmt=csv-noheader`
            res=$(mysql $db -N -e "SELECT sdbid FROM catwise WHERE name='$name';")
            if [ "$res" != "" ]
            then
                echo "Removing previous entry for $name ($res)"
                mysql $db -N -e "DELETE FROM catwise WHERE sdbid = '$res';"
            fi
            echo "Writing to $db.catwise"
            $stilts tpipe in=$ftmp ifmt=votable cmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=catwise write=dropcreate
        fi
    fi
else
    echo "  $sdbid already present"
fi
