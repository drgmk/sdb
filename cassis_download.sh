#!/bin/sh

# get CASSIS spectra for aors matched against db targets
py=/usr/local/bin/python3

# this is where spectra will go
root='/Users/grant/a-extra/sdb/spectra/'
ddir=$root`echo "import config; print(config.spectra['irsstare'][0])" | $py`
r1=`echo "import config; print(config.spectra['irsstare'][1])" | $py`
r2=`echo "import config; print(config.spectra['irsstare'][2])" | $py`

cd $ddir
echo "Downloading spectra to "$ddir
mysql $db -N -e "SELECT aor_key FROM sdb.spectra" | while read aor
do

    url1="https://cassis.sirtf.com/atlas/cgi/"
    url2="download.py?aorkey="$aor"&ptg=0&product=yaaar_oe_wavesamp"

    # see if file exists already or if we failed before
    if [ -e $ddir$r1$aor$r2 ]
    then
        echo "File "`ls $ddir$r1$aor$r2`" exists"
    elif [ -e $ddir$url2 ]
    then
        echo "Previous attempt at this file failed"

    # otherwise try to grab it from CASSIS
    else
        echo "Getting file for aor:"$aor
        url=$url1$url2
        echo $url
        curl -O -J $url
        sleep 5
    fi
done

cd -
