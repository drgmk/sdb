#!/usr/bin/env python3

"""Extract photometry from command line

When run from command line the arguments can either specify a number of
individual sdbids, or a sample which resides in a table in
sed_db_samples.
"""        

import argparse
import re
from collections import OrderedDict
from os import mkdir,rename,remove,utime
from os.path import isdir,isfile,getmtime,basename,dirname
import glob
import hashlib
import filelock
import numpy as np
from astropy.table import Table,vstack,unique,Column
import mysql.connector
import config as cfg

import sdf.filter


def sdb_write_rawphot(file,tphot,tspec):
    """Write raw photometry to a file.
    
    Takes filename to write to, and astropy Tables with photometry and
    information on spectra. Each contains metadata that is printed out
    as key=value. As the spectra are printed as instrument=file these
    are stored in reverse in the meta dictionary (i.e. instrument
    non-unique)

    TODO: Table.write can do the meta, they need to be "comments" and
    "keywords" entries in the meta dict.
    """
    
    fh = open(file,'w')
    print('\ photometry/spectra for '+tphot.meta['keywords']['id']['value']+'\n\\',file=fh)
    tphot.write(fh,format='ascii.ipac')
    fh.close()


def filehash(file):
    """Return an md5 hash for a given file, ignoring IPAC comments."""
    hasher = hashlib.md5()
    buf = b''
    with open(file,'r') as f:
        for text in f:
            if len(text) <= 2: # "\" and newline at a minimum
                pass
            elif text[0:2] == '\ ':
                pass
            else:
#                print(len(text),"|"+text+"|")
                buf += text.encode()

    hasher.update(buf)
    return hasher.hexdigest()


def sdb_getphot_one(id):
    """Extract photometry and other info from a database.
    
    Results are put in text files within sub-directories for the given
    id. Assuming some photometry exists, a 'public' directory is always
    created, and as many "private" ones as needed are created to ensure
    that private data are separated from public data and each other. The
    private directories also get an .htaccess file that requires a user
    to be in the group of the same name to view the contents.    
    """

    # set up connection
    cnx = mysql.connector.connect(user=cfg.mysql['user'],
                                  password=cfg.mysql['passwd'],
                                  host=cfg.mysql['host'],
                                  database=cfg.mysql['db'])
    cursor = cnx.cursor(buffered=True)

    # set up temporary table with what we'll want in the output
    cursor.execute("CREATE TEMPORARY TABLE fluxes ( Band varchar(10) NOT NULL DEFAULT '', Phot double DEFAULT NULL, Err double DEFAULT 0.0, Sys double DEFAULT 0.0, Lim int(1) NOT NULL DEFAULT '0', Unit varchar(10) NOT NULL DEFAULT '', bibcode varchar(19) NOT NULL DEFAULT '', Note1 varchar(100) NOT NULL DEFAULT '', Note2 varchar(100) NOT NULL DEFAULT '',SourceID varchar(100) DEFAULT NULL, private int(1) NOT NULL DEFAULT '0', exclude int(1) NOT NULL DEFAULT '0');")

    # get sdbid and xids
    cursor.execute('SELECT DISTINCT sdbid FROM xids WHERE xid=%(tmp)s;',{'tmp':id})
    if cursor.rowcount > 1:
        print("Found multiple sdbids for given ID in xids {}, exiting".format(id))
        exit()
    elif cursor.rowcount == 0:
        print("No sdbid for ID in xids {}, exiting".format(id))
        exit()

    sdbid = cursor.fetchall()[0][0]

    # make cross id table to match on
    cursor.execute('DROP TABLE IF EXISTS TheIDs;')
    cursor.execute('CREATE TEMPORARY TABLE TheIDs SELECT * from xids WHERE sdbid = %(tmp)s;',{'tmp':sdbid})
    cursor.execute('ALTER TABLE TheIDs ADD INDEX (xid);')

    # now add photometry to that table, use extra cursor
    cursor1 = cnx.cursor(buffered=True)
    cursor1.execute('SELECT * FROM xmatch;')
    for (incl,table,xid,band,col,ecol,sys,lim,bib,unit,c1,c2,
         excl,excl_join,extra,priv) in cursor1:
        if incl != 1:
            continue
        # columns to select
        stmt = 'Insert INTO fluxes SELECT DISTINCT '+band+', '+col+', '+ecol+', '+sys+', '+lim+', %(unit)s, '+bib+', '+c1+', '+c2+', TheIDs.xid, %(priv)s'
        # excludes
        stmt += ',IF(0 '
        # table excludes
        if excl_join != None:
            stmt += ' OR IF(exclude_band IS NULL,0,1)'
        # column based excludes
        if excl != None:
            stmt += ' OR '+excl
        stmt += ',1,0)'
        # main join
        stmt += ' FROM TheIDs LEFT JOIN '+table+' ON TheIDs.xid = '+xid
        # join exlude table if exists
        if excl_join != None:
            stmt += ' LEFT JOIN phot_exclude ON ('+excl_join+'=phot_exclude.join_id AND '+band+'=phot_exclude.exclude_band AND '+bib+'=phot_exclude.exclude_ref)'
        # require flux column not null
        stmt += ' WHERE '+col+' IS NOT NULL'
        # any extra conditions for WHERE
        if extra != None:
           stmt += ' '+extra
        cursor.execute(stmt,{'band':band,'bib':bib,'unit':unit,'priv':priv} )
        
    # now get the fluxes
    cursor.execute("SELECT DISTINCT * FROM fluxes;")
    tphot = Table(names=cursor.column_names,
                  dtype=('S10','f','f','f','i1','S10','S25','S200','S200','S100','i1','i1'))
    for row in cursor:
        try:
            tphot.add_row(row)
        except:
            print("ERROR: "+sdbid+" not exported, returning.")
            print("Probably wierd (+meaningless) characters in row\n",row)
            return()

    # fix any NULL values that went to nan
    tphot['Err'][np.invert(np.isfinite(tphot['Err']))] = 0.0
    tphot['Sys'][np.invert(np.isfinite(tphot['Sys']))] = 0.0

    # add wavelengths, sort, and remove
    tphot['wave'] = np.zeros(len(tphot))
    for i,filt in enumerate(tphot['Band']):
        if sdf.filter.iscolour(filt.decode()):
            f = sdf.filter.Colour.get(filt.decode())
        else:
            f = sdf.filter.Filter.get(filt.decode())

        tphot['wave'][i] = f.mean_wavelength

    tphot.sort('wave')
    tphot.remove_column('wave')

    # ready these to receive meta
    tphot.meta['keywords'] = {}
    tphot.meta['comments'] = []

    # get some addtional stellar data
    cursor.execute("SELECT main_id,raj2000,dej2000,sp_type,sp_bibcode,COALESCE(gaia.plx,simbad.plx_value) AS plx_value,COALESCE(gaia.e_plx,simbad.plx_err) AS plx_err,COALESCE(IF(gaia.plx IS NULL,NULL,'2016yCat.1337....0G'),plx_bibcode) AS plx_bibcode FROM sdb_pm LEFT JOIN simbad USING (sdbid) LEFT JOIN gaia USING (sdbid) where sdbid=%(tmp)s;",{'tmp':sdbid})
    vals = cursor.fetchall()
    keys = cursor.column_names
    if len(vals) > 0:
        tphot.meta['keywords'] = OrderedDict( zip(keys,tuple(vals[0])) )
    tphot.meta['keywords']['id'] = sdbid

    # look in xids for an id if nothing from simbad table
    if tphot.meta['keywords']['main_id'] is None:
        cursor.execute("SELECT DISTINCT xid FROM xids WHERE sdbid='{}' "
                       "ORDER BY xid LIMIT 1".format(sdbid))
        if cursor.rowcount == 1:
            tphot.meta['keywords']['main_id'] = cursor.fetchall()[0][0]

    # get aors for any spectra and add file names
    cursor.execute('SELECT instrument,aor_key,bibcode,private,IFNULL(exclude,0) as exclude FROM spectra LEFT JOIN spectra_exclude USING (aor_key,instrument) WHERE sdbid = %(tmp)s ORDER BY aor_key DESC;',{'tmp':sdbid})
    tspec = Table(names=cursor.column_names,dtype=('S20','i8','S19','i1','i1'))
    for row in cursor:
        tspec.add_row(row)

    cursor.close()
    cnx.close()

    # mark photometry duplicates to be excluded from fitting
    bs,js,ns = np.unique(tphot['Band'],
                          return_inverse=True,return_counts=True)
    for i,(band,n) in enumerate(zip(bs,ns)):
        if n == 1 or band.decode() not in cfg.phot['merge_dupes']:
            continue

        # indices where this band are in the phot table
        bib_i = np.where(js == i)[0]
        bibcodes = tphot['bibcode'][bib_i]
        srt = np.flipud(np.array(np.argsort(bibcodes)))
        # reversed argsort of these indices, this puts the highest dates
        # first, but alphabetical entries come first at this point
        srt = bib_i[srt]

        # now shift any non-[0-9] starting indices to the end
        r = re.compile('^[0-9]')
        vmatch = np.vectorize(lambda x:bool(r.match(x.decode())))
        num = vmatch(tphot['bibcode'][srt])
        if np.sum(num) < len(srt) and np.sum(num) > 0:
            num = np.where(num)[0]
            srt = np.roll(srt,len(srt)-np.min(num))

        # set exclude for everything beyond the first entry, unless any
        # of these were alredy excluded, in which case everything beyond
        # the first included measurement is excluded
        one_ok = False
        for j in srt:
            if one_ok is False and tphot['exclude'][j] == 0:
                one_ok = True
            else:
                tphot['exclude'][j] = 1

    # add empty column for file names
    add = np.chararray(len(tspec),itemsize=200)
    add[:] = ''
    tspec.add_column(Column(add,name='file'))
    for aor in tspec['aor_key']:
        for inst in cfg.spectra.keys():
            loc,g1,g2 = cfg.spectra[inst]
            specfile = glob.glob(cfg.file['spectra']+loc+g1+str(aor)+g2)
            if len(specfile) > 1:
                print("WARNING: More than one file for aor:",aor)
            elif len(specfile) == 1:
                file_path = loc+basename(specfile[0])
                tspec['file'][(tspec['aor_key']==aor)] = file_path

    # now add these as table metadata, adding a number to the instrument
    # name so that the keys are unique
    for i in range(len(tspec)):
        if tspec['file'][i] != b'':
            if tspec['exclude'][i]:
                tphot.meta['comments'].append(tspec['instrument'][i].decode()+str(i)+'='+tspec['file'][i].decode())
            else:
                tphot.meta['keywords'][tspec['instrument'][i].decode()+str(i)] = tspec['file'][i].decode()

    # rearrange to ensure keyword structure is correct for IPAC ascii
    # format, which is {'keywords':{'keyword': {'value': value} }
    for key in tphot.meta['keywords'].keys():
        tphot.meta['keywords'][key] = {'value':tphot.meta['keywords'][key]}

    # find any private data by making table from phot and spec,
    # bibcode and private are the common columns
    tpriv = vstack([tphot,tspec],join_type='inner')
    if len(tpriv[tpriv['private'] == 1]) == 0:
        npriv = 0
    else:
        npriv = len(unique(tpriv[tpriv['private'] == 1]))

    # if there wasn't any photometry or spectra, then there's nothing to do
    if len(tpriv) == 0:
        print("No photometry or spectra for {}, skipping".format(sdbid))
        return
        
    # write file(s), organising things if there is private data
    sedroot = cfg.file['sedroot']+sdbid+'/'
    if isdir(sedroot) == False:
        mkdir(sedroot)
    filename = sdbid+'-rawphot.txt'

    # make list of new dirs needed, 'public' will contain only public
    # photometry, 'all' will contain everything and is only needed if 
    # there are multiple private sets of photometry
    newdirs = np.array(['public'])
    if npriv > 0:
        newdirs = np.append(newdirs,unique(tpriv[tpriv['private'] == 1])['bibcode'])
    if len(newdirs) > 2:
        newdirs = np.append(newdirs,'all')

    # go through samples, updating only if different
    cur = glob.glob(sedroot+'*/')
    for dir in newdirs:
        seddir = sedroot+dir+'/'

        # make dir if needed, if exists get or invent hash on old file
        oldhash = ''
        if isdir(seddir) == False:
            mkdir(seddir)
            mtime = getmtime(seddir)
        else:
            if isfile(seddir+filename):
                oldhash = filehash(seddir+filename)
                mtime = getmtime(seddir+filename)

        # create the .htaccess file if necessary
        if not isfile(seddir+'/.htaccess') and dir != 'public':
            fd = open(seddir+'/.htaccess','w')
            fd.write('AuthName "Must login"\n')
            fd.write('AuthType Basic\n')
            fd.write('AuthUserFile '+cfg.www['root']+'.htpasswd\n')
            fd.write('AuthGroupFile '+cfg.www['root']+'.htgroup\n')
            fd.write('require group admin '+dir+'\n')
            fd.close()

        # only write if we can get a lock, sdf may be fitting the old file
        lock = filelock.FileLock(seddir+'/.sdf_lock-'+filename)
        try:
            with lock.acquire(timeout = 0):

                # figure which rows to keep, and write temporary file
                okphot = np.logical_or(tphot['private'] == 0,
                                       tphot['bibcode'].astype(str) == dir.astype(str) )
                okspec = np.logical_or(tspec['private'] == 0,
                                       tspec['bibcode'].astype(str) == dir.astype(str) )
                tmpfile = cfg.file['sedtmp']
                if dir != 'all':
                    sdb_write_rawphot(tmpfile,tphot[okphot],tspec[okspec])
                else:
                    sdb_write_rawphot(tmpfile,tphot,tspec)

                # see if update needed, if so move it, otherwise delete
                if filehash(tmpfile) != oldhash:
                    if oldhash != '':
                        print(sdbid,dir,": Different hash, updating file")
                    else:
                        print(sdbid,dir,": Creating file")
                    rename(tmpfile,seddir+filename)

                    # hack to retain mod time of old file
        #            utime(seddir+filename,times=(mtime,mtime))
                else:
                    print(sdbid,dir,": Same hash, leaving old file")
                    remove(tmpfile)

                # remove this dir listing if it's there
                if seddir in cur:
                    cur.remove(seddir)

            # check for orphaned SED directories
            if len(cur) > 0:
                print("\nWARNING: orphaned directories exist:",cur,"\n")

        except filelock.Timeout:
            print(" WARNING: can't acquire lock for {}".format(dir+'/'+filename))
            print("  skipping")


def sdb_getphot(idlist):
    """Call sdb_getphot for a list of ids."""
    
    if not isinstance(idlist, list):
        print("sdb_getphot expects a list")
        raise TypeError
    for id in idlist:
        sdb_getphot_one(id)


# run from the command line
if __name__ == "__main__":

    # inputs
    parser1 = argparse.ArgumentParser(description='Export photometry from SED DB')
    parser = parser1.add_mutually_exclusive_group(required=True)
    parser.add_argument('--idlist','-i',nargs='+',action='append',
                        dest='idlist',metavar='sdbid',help='Get photometry for one sdbid')
    parser.add_argument('--sample','-s',type=str,metavar='table',help='Get photometry for sample')
    parser.add_argument('--all','-a',action='store_true',help='Get all photometry')
    parser1.add_argument('--dbname',type=str,help='Database containing sample table',
                         default=cfg.mysql['sampledb'],metavar=cfg.mysql['sampledb'])
    args = parser1.parse_args()

    if args.idlist != None:
        print("Running sdb_getphot for list:",args.idlist[0])
        sdb_getphot(args.idlist[0])
    elif args.sample != None:
        print("Running sdb_getphot for targets in sample:",args.sample)
        cnx = mysql.connector.connect(user=cfg.mysql['user'],password=cfg.mysql['passwd'],
                                      host=cfg.mysql['host'],database=args.dbname)
        cursor = cnx.cursor(buffered=True)
        cursor.execute("SELECT sdbid FROM "+args.dbname+"."+args.sample+" "
                       "WHERE sdbid IS NOT NULL;")
        for id in cursor:
            sdb_getphot_one(id[0])
        cursor.close()
        cnx.close()
    elif args.all != None:
        print("Running sdb_getphot for all targets")
        cnx = mysql.connector.connect(user=cfg.mysql['user'],password=cfg.mysql['passwd'],
                                      host=cfg.mysql['host'],database='sdb')
        cursor = cnx.cursor(buffered=True)
        cursor.execute("SELECT sdbid FROM sdb_pm;")
        for id in cursor:
            sdb_getphot_one(id[0])
        cursor.close()
        cnx.close()
