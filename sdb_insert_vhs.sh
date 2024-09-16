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

# Gaia, query against 2016.0 position
echo "\nLooking for VHS entry"
res=$(mysql $db -N -e "SELECT sdbid FROM vhs WHERE sdbid='$sdbid';")
if [ "$res" == "" ]
then
    epoch=2013.0
    co=$(mysql $db -N -e "SELECT CONCAT(raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600.,',',dej2000 + ($epoch-2000.0) * pmde/1e3/3600.) from sdb_pm where sdbid = '$sdbid';")

    if [ "$co" != "" ]
    then
        echo $co
        fviz=/tmp/pos$RANDOM.txt
        ftmp=/tmp/pos$RANDOM.txt
        vizquery -site=$site -mime=votable -source=II/367/vhs_dr5 -c.rs=$rad -sort=_r -out.max=1 -out.add=_r -out.add=e_Yap3 -out.add=e_Jap3 -out.add=e_Hap3 -out.add=e_Ksap3 -out.add=pStar -out.add=pNoise -c="$co" > $fviz
        tout=`$stilts tjoin nin=2 in1=$fid ifmt1=ascii icmd1='keepcols sdbid' in2=$fviz ifmt2=votable icmd2='colmeta -name Y_Jpnt Y-Jpnt' icmd2='colmeta -name J_Hpnt J-Hpnt' icmd2='colmeta -name H_Kspnt H-Kspnt' icmd2='colmeta -name J_Kspnt J-Kspnt' icmd2='colmeta -name Y_Jext Y-Jext' icmd2='colmeta -name J_Hext J-Hext' icmd2='colmeta -name H_Ksext H-Ksext' icmd2='colmeta -name J_Ksext J-Ksext' ocmd='random' omode=out ofmt=votable 2>&1 > $ftmp`

        if [[ "$tout" == "Error: No TABLE element found" ]]
        then
            echo "Nothing found, writing empty entry in $db.vhs"
            $(mysql $db -N -e "INSERT INTO $db.vhs (sdbid) VALUES ('$sdbid');")
        else
            # check if there's an entry for this source already
            src=`$stilts tpipe in=$ftmp ifmt=votable cmd=random cmd='keepcols name' omode=out ofmt=csv-noheader`
            res=$(mysql $db -N -e "SELECT sdbid FROM vhs WHERE name='$src';")
            if [ "$res" != "" ]
            then
                echo "Removing previous entry for $src ($res)"
                mysql $db -N -e "DELETE FROM vhs WHERE sdbid = '$res';"
            fi
            echo "Writing to $db.vhs"
            $stilts tpipe in=$ftmp ifmt=votable cmd='random' omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=vhs write=append
        fi
    fi
else
    echo "  $sdbid already present"
fi
