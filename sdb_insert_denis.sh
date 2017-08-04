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

# DENIS, rough mean epoch of 1997
echo "\nLooking for DENIS entry"
res=$(mysql $db -N -e "SELECT sdbid FROM denis WHERE sdbid='$sdbid';")
if [ "$res" == "" ]
then
    epoch=1997.0
    co=$(mysql $db -N -e "SELECT CONCAT(raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600.,',',dej2000 + ($epoch-2000.0) * pmde/1e3/3600.) from sdb_pm where sdbid = '$sdbid';")

    if [ "$co" != "" ]
    then
        echo $co
        fviz=/tmp/pos$RANDOM.txt
        ftmp=/tmp/pos$RANDOM.txt
        vizquery -site=$site -mime=votable -source=B/denis/denis -c.rs=$rad -sort=_r -out.max=1 -out.add=_r -out.add=q_Imag -out.add=q_Jmag -out.add=q_Kmag -out.add=Iflg -out.add=Jflg -out.add=Kflg -out.add=mult -c="$co" > $fviz
        tout=`$stilts tjoin nin=2 in1=$fid ifmt1=ascii icmd1='keepcols sdbid' in2=$fviz ifmt2=votable ocmd='random' omode=out ofmt=votable 2>&1 > $ftmp`

        if [[ "$tout" == "Error: No TABLE element found" ]]
        then
            echo "Nothing found, writing empty entry in $db.denis"
            $(mysql $db -N -e "INSERT INTO $db.denis (sdbid) VALUES ('$sdbid');")
        else
            # check if there's an entry for this source already
            denis=`$stilts tpipe in=$ftmp ifmt=votable cmd=random cmd='keepcols denis' omode=out ofmt=csv-noheader`
            res=$(mysql $db -N -e "SELECT sdbid FROM denis WHERE denis='$denis';")
            if [ "$res" != "" ]
            then
                echo "Removing previous entry for $denis ($res)"
                mysql $db -N -e "DELETE FROM denis WHERE sdbid = '$res';"
            fi
            echo "Writing to $db.denis"
            $stilts tpipe in=$ftmp ifmt=votable cmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=denis write=append
        fi
    fi
else
    echo "  $sdbid already present"
fi
