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

# 2MASS, mean epoch of 1999.3, midway through survey 2006AJ....131.1163S
echo "\nLooking for 2MASS entry"
res=$(mysql $db -N -e "SELECT sdbid FROM 2mass WHERE sdbid='$sdbid';")
if [ "$res" == "" ]
then
    epoch=1999.3
    co=$(mysql $db -N -e "SELECT CONCAT(raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600.,',',dej2000 + ($epoch-2000.0) * pmde/1e3/3600.) from sdb_pm where sdbid = '$sdbid';")

    if [ "$co" != "" ]
    then
        echo $co
        fviz=/tmp/pos$RANDOM.txt
        ftmp=/tmp/pos$RANDOM.txt
        vizquery -site=$site -mime=votable -source=II/246/out -c.rs=$rad -sort=_r -out.max=1 -out.add=_r -c="$co" > $fviz
        tout=`$stilts tjoin nin=2 in1=$fid ifmt1=ascii icmd1='keepcols sdbid' in2=$fviz ifmt2=votable ocmd='random' omode=out ofmt=votable 2>&1 > $ftmp`

        if [[ "$tout" == "Error: No TABLE element found" ]]
        then
            echo "Nothing found, writing empty entry in $db.2mass"
            $(mysql $db -N -e "INSERT INTO $db.2mass (sdbid) VALUES ('$sdbid');")
        else
            # check if there's an entry for this source already
            tmass=`$stilts tpipe in=$ftmp ifmt=votable cmd=random cmd='keepcols 2mass' omode=out ofmt=csv-noheader`
            res=$(mysql $db -N -e "SELECT sdbid FROM 2mass WHERE 2mass='$tmass';")
            if [ "$res" != "" ]
            then
                echo "Removing previous entry for $tmass ($res)"
                mysql $db -N -e "DELETE FROM 2mass WHERE sdbid = '$res';"
            fi
            echo "Writing to $db.2mass"
            $stilts tpipe in=$ftmp ifmt=votable cmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=2mass write=append
        fi
    fi
else
    echo "  $sdbid already present"
fi
