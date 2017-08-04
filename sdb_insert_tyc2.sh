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

# Tycho-2, query against 1991.25 position. add integer version of tyc2
# id for matching
echo "\nLooking for Tycho-2 entry"
res=$(mysql $db -N -e "SELECT sdbid FROM tyc2 WHERE sdbid='$sdbid';")
if [ "$res" == "" ]
then
    epoch=1991.25
    co=$(mysql $db -N -e "SELECT CONCAT(raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600.,',',dej2000 + ($epoch-2000.0) * pmde/1e3/3600.) from sdb_pm where sdbid = '$sdbid';")

    if [ "$co" != "" ]
    then
        echo $co
        fviz=/tmp/pos$RANDOM.txt
        ftmp=/tmp/pos$RANDOM.txt
        vizquery -site=$site -mime=votable -source=I/259/tyc2 -c.rs=$rad -sort=_r -out.max=1 -out.add=_r -out.add=e_BTmag -out.add=e_VTmag -out.add=prox -out.add=CCDM -c="$co" > $fviz
        tout=`$stilts tjoin nin=2 in1=$fid ifmt1=ascii icmd1='keepcols sdbid' in2=$fviz ifmt2=votable icmd2='colmeta -name RA_ICRS_ RA(ICRS)' icmd2='colmeta -name DE_ICRS_ DE(ICRS)' ocmd='random' ocmd='addcol -before _r tyc2id concat(toString(tyc1),concat(\"-\",concat(toString(tyc2),concat(\"-\",toString(tyc3)))))' ocmd='random' omode=out ofmt=votable 2>&1 > $ftmp`

        if [[ "$tout" == "Error: No TABLE element found" ]]
        then
            echo "Nothing found, writing empty entry in $db.tyc2"
            $(mysql $db -N -e "INSERT INTO $db.tyc2 (sdbid) VALUES ('$sdbid');")
        else
            # check if there's an entry for this source already
            tyc2id=`$stilts tpipe in=$ftmp ifmt=votable cmd=random cmd='keepcols tyc2id' omode=out ofmt=csv-noheader`
            res=$(mysql $db -N -e "SELECT sdbid FROM tyc2 WHERE tyc2id='$tyc2id';")
            if [ "$res" != "" ]
            then
                echo "Removing previous entry for $tyc2id ($res)"
                mysql $db -N -e "DELETE FROM tyc2 WHERE sdbid = '$res';"
            fi
            echo "Writing to $db.tyc2"
            $stilts tpipe in=$ftmp ifmt=votable cmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=tyc2 write=append
        fi
    fi
else
    echo "  $sdbid already present"
fi
