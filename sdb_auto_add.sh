#!/bin/sh

# run when files added to auto_add dir, this is launched with a launchd plist by
# launchctl load ~/Library/LaunchAgents/sdb_auto_watch.plist, which watches the auto_add
# folder. Upon addition of a file this script is launched. The LaunchAgents folder also
# contains plist files to set the enviroment variables PATH and IDL_STARTUP

root=/Users/grant/astro/projects/sdb/sdb/
pushd $root
logroot=/Users/grant/a-extra/sdb/log/
add=$logroot../auto_add/add.txt

if [ -e $add ]
then

    while read -r line
    do
	id=${line//[^a-zA-Z0-9-+ ]/}
	echo "Got:""$id"
	logname=${id//[^a-zA-Z0-9-+]/}_$(date +"%Y%m%d-%H%M%S")
	./sdb_auto_add_one.sh "$id" &> "$logroot""$logname".log
	
    done < $add
    
    rm $add
fi

#osascript -e "tell Application \"System Events\" to display alert \"$id added to sdb\""

popd
