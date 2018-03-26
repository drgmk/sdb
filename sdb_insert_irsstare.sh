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

# look for IRS spectra in the observing log, 5" crossmatch
echo "\nLooking for IRS staring observation in Spitzer log"
res=$(mysql $db -N -e "SELECT sdbid FROM spectra WHERE sdbid='$sdbid' AND instrument='irsstare';")
if [ "$res" == "" ]
then
    epoch=2006.9
    irsra=$(mysql $db -N -e "SELECT raj2000 + ($epoch-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600. from sdb_pm where sdbid = '$sdbid';")
    irsde=$(mysql $db -N -e "SELECT dej2000 + ($epoch-2000.0) * pmde/1e3/3600. from sdb_pm where sdbid = '$sdbid';")

    if [ "$irsra" != "" ]
    then
        echo $irsra,$irsde
        firs=/tmp/pos$RANDOM.txt
        ftmp=/tmp/pos$RANDOM.txt

        $stilts sqlclient db='jdbc:mysql://localhost/'$sdb user=$user password=$password sql="SELECT sdbid,raj2000 + (2006.9-2000.0) * pmra/1e3/cos(dej2000*pi()/180.0)/3600. as ra_ep2006p9,dej2000 + (2006.9-2000.0) * pmde/1e3/3600. as de_ep2006p9 from sdb_pm where sdbid = '$sdbid'" ofmt=votable > $ftmp

        $stilts sqlclient db='jdbc:mysql://localhost/photometry'$ssl user=$user password=$password sql="SELECT name,ra,dec_,aor_key,'irsstare' as instrument, '2011ApJS..196....8L' as bibcode, 0 as private from spitzer_obslog where ra between $irsra-5.0 and $irsra+5.0 and dec_ between $irsde-5.0 and $irsde+5.0 and aot='irsstare'" ofmt=votable > $firs

        # check if there's an entry for this source already
        instaor=`$stilts tpipe in=$ftmp ifmt=votable cmd=random cmd='addcol -before _r instaor concat(instrument,toString(aor_key))' cmd='keepcols instaor' omode=out ofmt=csv-noheader`
        res=$(mysql $db -N -e "SELECT sdbid FROM spectra WHERE CONCAT(instrument,aor_key)='$instaor';")
        if [ "$res" != "" ]
        then
            echo "Removing previous entry for $instaor ($res)"
            mysql $db -N -e "DELETE FROM spectra WHERE sdbid = '$res';"
        fi

        $stilts tmatch2 in1=$ftmp ifmt1=votable in2=$firs ifmt2=votable ocmd='keepcols "sdbid instrument aor_key bibcode private"' matcher=skyellipse values1='ra_ep2006p9 de_ep2006p9 6.0 6.0 0.0' values2='ra dec_ 6.0 6.0 0.0' params='2' find=all omode=tosql protocol=mysql db=$sdb user=$user password=$password dbtable=spectra write=append
    fi
else
    echo "  $sdbid already present"
fi
