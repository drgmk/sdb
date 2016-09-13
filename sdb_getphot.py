#!/usr/bin/env python3

"""Extract photometry from command line

When run from command line the arguments can either specify a number of
individual sdbids, or a sample which resides in a table in
sed_db_samples.
"""        

import argparse
from collections import OrderedDict
import numpy as np
from os import mkdir,rename,remove
from os.path import isdir,isfile
import glob
import hashlib
from astropy.table import Table,vstack,unique,Column
import mysql.connector
import config as cfg

def sdb_write_rawphot(file,tphot,tspec):
    """Write raw photometry to a file
    
    Takes filename to write to, and astropy Tables with photometry and
    information on spectra. Each contains metadata that is printed out
    as key=value. As the spectra are printed as instrument=file these
    are stored in reverse in the meta dictionary (i.e. instrument
    non-unique)
    """
    
    fh = open(file,'w')
    print('# photometry etc for '+tphot.meta['id']+'\n',file=fh)
    for key in tphot.meta:
        print("{}={}".format(key,tphot.meta[key]),file=fh)
    for key in tspec.meta:
        print("{}={}".format(tspec.meta[key],key),file=fh)
    print('',file=fh)
    if len(tphot) > 0:
        tphot.rename_column('Band','#Band')
        tphot.write(fh,format='ascii.fixed_width',delimiter='|')
        tphot.rename_column('#Band','Band')
    fh.close()

def filehash(file):
    """Return an md5 hash for a given file"""
    hasher = hashlib.md5()
    f = open(file, 'rb')
    buf = f.read()
    hasher.update(buf)
    return hasher.hexdigest()

def sdb_getphot_one(id):
    """Extract photometry and other info from a database
    
    Results are put in text files within sub-directories for the given
    id. Assuming some photometry exists, a 'public' directory is always
    created, and as many "private" ones as needed are created to ensure
    that private data are separated from public data and each other. The
    private directories also get an .htaccess file that requires a user
    to be in the group of the same name to view the contents.    
    """

    # set up connection
    cnx = mysql.connector.connect(user=cfg.mysql['user'],password=cfg.mysql['passwd'],
                                  host=cfg.mysql['host'],database=cfg.mysql['db'])
    cursor = cnx.cursor(buffered=True)

    # set up temporary table with what we'll want in the photometry output
    cursor.execute("CREATE TEMPORARY TABLE fluxes ( Band varchar(10) NOT NULL DEFAULT '', Phot double DEFAULT NULL, Err double DEFAULT NULL, Sys double DEFAULT NULL, Lim int(1) NOT NULL DEFAULT '0', Unit varchar(10) NOT NULL DEFAULT '', bibcode varchar(19) NOT NULL DEFAULT '', Note1 varchar(100) NOT NULL DEFAULT '', Note2 varchar(100) NOT NULL DEFAULT '',SourceID varchar(100) DEFAULT NULL, private int(1) NOT NULL DEFAULT '0');")

    # get sdbid and xids
    cursor.execute('SELECT DISTINCT sdbid FROM xids WHERE xid=%(tmp)s;',{'tmp':id})
    if cursor.rowcount > 1:
       print("Found multiple sdbids for given ID {}, exiting".format(id))
       exit()
    sdbid = cursor.fetchall()[0][0]

    # make cross id table to match on
    cursor.execute('DROP TABLE IF EXISTS TheIDs;')
    cursor.execute('CREATE TEMPORARY TABLE TheIDs SELECT * from xids WHERE sdbid = %(tmp)s;',{'tmp':sdbid})
    cursor.execute('ALTER TABLE TheIDs ADD INDEX (xid);')

    # now add photometry to that table, use extra cursor
    cursor1 = cnx.cursor(buffered=True)
    cursor1.execute('SELECT * FROM xmatch;')
    for (incl,table,xid,band,col,ecol,sys,lim,bib,unit,c1,c2,ex,priv) in cursor1:
        if incl != 1:
            continue
        stmt = 'Insert INTO fluxes SELECT DISTINCT '+band+', '+col+', '+ecol+', '+sys+', '+lim+', %(unit)s, '+bib+', '+c1+', '+c2+', TheIDs.xid, %(priv)s FROM TheIDs LEFT JOIN '+table+' ON TheIDs.xid = '+xid+' WHERE '+col+' IS NOT NULL'
        if ex != '':
           stmt += ' '+ex
        cursor.execute(stmt,{'band':band,'bib':bib,'unit':unit,'priv':priv} )
        
    # now get the fluxes
    cursor.execute("SELECT DISTINCT * FROM fluxes;")
    tphot = Table(names=cursor.column_names,dtype=('S10','f','f','f','i1','S10','S25','S200','S200','S100','bool'))
    for row in cursor:
        try:
            tphot.add_row(row)
        except:
            print("ERROR: "+sdbid+" not exported, returning.")
            print("Probably wierd (+meaningless) characters in row\n",row)
            return()

    # get some addtional metadata like stellar params
    cursor.execute('SELECT main_id,sp_type,sp_bibcode,plx_value,plx_err,plx_bibcode from simbad WHERE sdbid=%(tmp)s;',{'tmp':sdbid})
    vals = cursor.fetchall()
    keys = cursor.column_names
    if len(vals) > 0:
        tphot.meta = OrderedDict( zip(keys,tuple(vals[0])) )
    tphot.meta['id'] = sdbid

    # get aors for any spectra and add file names
    cursor.execute('SELECT instrument,aor_key,bibcode,private FROM spectra WHERE sdbid = %(tmp)s ORDER BY aor_key DESC;',{'tmp':sdbid})
    tspec = Table(names=cursor.column_names,dtype=('S20','i8','S19','bool'))
    for row in cursor:
        tspec.add_row(row)

    cursor.close()
    cnx.close()

    # add empty column for file names
    add = np.chararray(len(tspec),itemsize=200)
    add[:] = ''
    tspec.add_column(Column(add,name='file'))
    for aor in tspec['aor_key']:
        for inst in cfg.spectra.keys():
            loc,g1,g2 = cfg.spectra[inst]
            specfile = glob.glob(loc+g1+str(aor)+g2)
            if len(specfile) > 1:
                print("WARNING: More than one file for aor:",aor)
            elif len(specfile) == 1:
                tspec['file'][(tspec['aor_key']==aor)] = specfile[0]

    # now add these as table metadata
    for i in range(len(tspec)):
        if tspec['file'][i] != b'':
            tspec.meta[tspec['file'][i].decode()] = tspec['instrument'][i].decode()

    # find any private data by making table from phot and spec,
    # bibcode and private are the common columns
    tpriv = vstack([tphot,tspec],join_type='inner')
    if len(tpriv[tpriv['private'] == True]) == 0:
        npriv = 0
    else:
        npriv = len(unique(tpriv[tpriv['private'] == True]))

    # if there wasn't any photometry or spectra, then there's nothing to do
    if len(tpriv) == 0:
        print("No photometry or spectra for {}, exiting".format(sdbid))
        return
        
    # write file(s), organising things if there is private data
    sedroot = cfg.file['sedroot']+sdbid+'/'
    if isdir(sedroot) == False:
        mkdir(sedroot)
    filename = sdbid+'-rawphot.txt'
    # make list of new dirs needed, 'public' will contain only public photometry, 'all'
    # will contain everything and is only needed if there are multiple private sets of
    # photometry
    newdirs = np.array(['public'])
    if npriv > 0:
        newdirs = np.append(newdirs,unique(tpriv[tpriv['private'] == True])['bibcode'])
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
        else:
            if isfile(seddir+filename):
                oldhash = filehash(seddir+filename)

        # create the .htaccess file if necessary
        if not isfile(seddir+'/.htaccess') and dir != 'public':
            fd = open(seddir+'/.htaccess','w')
            fd.write('AuthName "Must login"\n')
            fd.write('AuthType Basic\n')
            fd.write('AuthUserFile '+cfg.www['root']+'.htpasswd\n')
            fd.write('AuthGroupFile '+cfg.www['root']+'.htgroup\n')
            fd.write('require group '+dir+'\n')
            fd.close()

        # figure which rows to keep, and write temporary file
        okphot = np.logical_or(tphot['private'] == False,
                               tphot['bibcode'].astype(str) == dir.astype(str) )
        okspec = np.logical_or(tspec['private'] == False,
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
        else:
            print(sdbid,dir,": Same hash, leaving old file")
            remove(tmpfile)

        # remove this dir listing if it's there
        if seddir in cur:
            cur.remove(seddir)

    # check for orphaned SED directories
    if len(cur) > 0:
        print("\nWARNING: orphaned directories exist:",cur,"\n")


def sdb_getphot(idlist):
    """Call sdb_getphot for a list of ids"""
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
        cursor.execute("SELECT sdbid FROM "+args.dbname+"."+args.sample+";")
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
