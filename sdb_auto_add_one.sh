#!/bin/sh

# automation of additions to sdb, relies on target being sesame/simbad resolvable for now

root=/Users/grant/astro/projects/sdb/sdb/
pushd $root
sedroot=`echo "import config; print(config.file['sedroot'])" | python`

# database details, user and pass are taken from /etc/my.cnf, and aren't needed for
# direct calls, but are needed explicitly for stilts.
db=sdb
ssl=?useSSL=false
sdb=$db$ssl
tmp=`cat /etc/my.cnf | grep user | sed 's/ //g'`
eval $tmp
tmp=`cat /etc/my.cnf | grep password | sed 's/ //g'`
eval $tmp

# other knobs as required
rad=2               # rad is the default match radius in arcsec
site=fr             # vizquery site

# the basic stilts command to use, no -Xms1g -Xmx1g since presumably little memory needed
stilts='/Applications/stilts -classpath /Library/Java/Extensions/mysql-connector-java-5.1.8-bin.jar -Djdbc.drivers=com.mysql.jdbc.Driver'

# sanitize input, only alphanumeric, pluses, minuses, and spaces should do it
idin=${1//[^a-zA-Z0-9-+ ]/}

# Look up with sesame
echo "Sesame using:$idin"
id="`sesame $idin | egrep -w 'oname' | sed s/\<oname\>// | sed s/\<\\\/oname\>//`"
id=`echo "$id" | sed "s/^ *//g"`
if [ "$id" == "" ]
then
    echo "Sesame found nothing for:""$1"
    exit
fi
echo "Sesame found:""$id"

# create a lock file to ensure this target isn't entered twice at the same time
lock=/tmp/${id//[^a-zA-Z0-9]/}.lock
if [ -e $lock ]
then
    echo "Lock $lock on $id exists, stopping"
    exit
else
    echo "Creating lock: $lock"
    touch $lock
fi

# add it to the db
./db-insert-one.sh "$id"

# skip trying to get spectra
#./cassis_download.sh

# extract the photometry, need to know sdbid for that
sdbid=$(mysql $db -N -e "SELECT sdbid FROM xids WHERE xid='$id';")
./sdb_getphot.py -i "$sdbid"
popd

# turn this into an IDL save file
pushd $sedroot
echo "sdb_csv2xdr,'$sdbid/public/$sdbid-rawphot.txt'" | idl

# run the SED fitting
echo "sedfitg,/AUTO,xdr='$sdbid/public/$sdbid.xdr',/COMPLETE" | idl
popd

# remove the lock file
rm $lock
