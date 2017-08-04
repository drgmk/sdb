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

# APASS, rough mean epoch of 2011
echo "\nLooking for APASS entry"
res=$(mysql $db -N -e "SELECT sdbid FROM apass WHERE sdbid='$sdbid';")
if [ "$res" == "" ]
then
    epoch=2011.0
    co=$(mysql $db -N -e "SELECT CONCAT(raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600.,',',dej2000 + ($epoch-2000.0) * pmde/1e3/3600.) from sdb_pm where sdbid = '$sdbid';")

    if [ "$co" != "" ]
    then
        echo $co
        fviz=/tmp/pos$RANDOM.txt
        ftmp=/tmp/pos$RANDOM.txt
        vizquery -site=$site -mime=votable -source=II/336/apass9 -c.rs=$rad -sort=_r -out.max=1 -out.add=_r -c="$co" > $fviz
        # would like to remove warnings created here, but columns have
        # quotes in them...
        tout=`$stilts tjoin nin=2 in1=$fid ifmt1=ascii icmd1='keepcols sdbid' in2=$fviz ifmt2=votable icmd2='colmeta -name e_B_V e_B-V' icmd2='colmeta -name B_V B-V' ocmd='random' omode=out ofmt=votable 2>&1 > $ftmp`

        if [[ "$tout" == "Error: No TABLE element found" ]]
        then
            echo "Nothing found, writing empty entry in $db.apass"
            $(mysql $db -N -e "INSERT INTO $db.apass (sdbid) VALUES ('$sdbid');")
        else
            # check if there's an entry for this source already
            recno=`$stilts tpipe in=$ftmp ifmt=votable cmd=random cmd='keepcols recno' omode=out ofmt=csv-noheader`
            res=$(mysql $db -N -e "SELECT sdbid FROM apass WHERE recno=$recno;")
            if [ "$res" != "" ]
            then
                echo "Removing previous entry for $recno ($res)"
                mysql $db -N -e "DELETE FROM apass WHERE sdbid = '$res';"
            fi
            echo "Writing to $db.apass"
            $stilts tpipe in=$ftmp ifmt=votable cmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=apass write=append
        fi
    fi
else
    echo "  $sdbid already present"
fi
