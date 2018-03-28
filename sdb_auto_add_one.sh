#!/bin/sh

# automation of additions to sdb, relies on target being sesame/simbad resolvable for now

root=/Users/grant/astro/projects/sdb/sdb/
pushd $root
sedroot=`echo "import config; print(config.file['sedroot'])" | python3`

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
echo "/~~~~~~ sdb_auto_add_one.sh ~~~~~~/"

# the basic stilts command to use, no -Xms1g -Xmx1g since presumably little memory needed
stilts='/Applications/stilts -classpath /Library/Java/Extensions/mysql-connector-java-5.1.8-bin.jar -Djdbc.drivers=com.mysql.jdbc.Driver'

# sanitize input, only alphanumeric, pluses, minuses, and spaces should do it
idin=${1//[^a-zA-Z0-9-+ ]/}

# Look up with sesame
echo "Sesame using:$idin"
id="`sesame $idin | egrep -w 'oname' | sed s/\<oname\>// | sed s/\<\\\/oname\>// | sed 's/^ *//g' | sed 's/  */ /g'`"

# try again if the id contains a quote
if [[ "$id" =~ "'" ]]
then
    id="`sesame -oxI1 $idin | egrep -w 'alias' | egrep -v "'" | head -1 | sed s/\<alias\>// | sed s/\<\\\/alias\>// | sed 's/^ *//g' | sed 's/ *$//g' | sed 's/  */ /g'`"
fi

# or try coord based query instead
if [ "$id" == "" ]
then
    echo "Fail, attempting to interpret as coords"
    co=`sesame "$idin" | egrep -w 'jradeg|jdedeg'`
    cojoin=${co//[$'\n']/,}
    cojoin=${cojoin//[^0-9+,\.]/}
    if [ "$cojoin" != "" ]
    then
	echo "Success, looks like a coordinate"
	id="$idin"
    fi
fi

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
echo "\n"
echo "/~~~~~~ sdb_getphot.py ~~~~~~/"
echo "using $id"
sdbid=$(mysql $db -N -e "SELECT sdbid FROM xids WHERE xid='$id';")
./sdb_getphot.py -i "$sdbid"
echo "------- sdb_getphot.py -------"
popd

# run fitting, do once without spectra for speed, and then redo with
# can't set DYLD_LIBRARY_PATH within launchctl
echo "\nRunning sdf"
pushd $sedroot
export DYLD_LIBRARY_PATH=/Users/grant/astro/code/github/MultiNest/lib
sdf-fit -f $sdbid/public/$sdbid-rawphot.txt --no-spectra -w -b
sdf-fit -f $sdbid/public/$sdbid-rawphot.txt -u -w -b
popd

# remove the lock file
rm $lock

echo "\nDone"
echo "------- sdb_auto_add_one.sh -------"
