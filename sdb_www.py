#!/Users/grant/.virtualenvs/astropy-dev/bin/python3
#!/usr/bin/env python3

"""Generate HTML pages to browse database"""

from os.path import isdir,isfile
from os import mkdir,remove,write
from astropy.table import Table,jsviewer
import argparse
import mysql.connector
import config as cfg

def sdb_www_samples():
    """Generate sample HTML pages"""

    wwwroot = cfg.www['root']+'samples/'

    # set up connection
    cnx = mysql.connector.connect(user=cfg.mysql['user'],password=cfg.mysql['passwd'],
                                  host=cfg.mysql['host'],database=cfg.mysql['db'])
    cursor = cnx.cursor(buffered=True)

    # get a list of projects, and add "all" and "public" lists ("public" might not show
    # everything in the list, but "all" would but might be restricted)
    cursor.execute("SELECT DISTINCT project FROM projects;")
    samples = cursor.fetchall()[0]
    samples = samples + ('public','all')

    # generate the pages for these samples
    for sample in samples:

        # make dir and .htaccess if dir doesn't exist
        if not isdir(wwwroot+sample):
            mkdir(wwwroot+sample)

        # make .htaccess if needed, don't put one in "public" so stuff in that directory
        # remains to those not logged in
        if not isfile(wwwroot+sample+'/.htaccess') and sample != 'public':
            fd = open(wwwroot+sample+'/.htaccess','w')
            fd.write('AuthName "Must login"\n')
            fd.write('AuthType Basic\n')
            fd.write('AuthUserFile '+cfg.www['root']+'.htpasswd\n')
            fd.write('AuthGroupFile '+cfg.www['root']+'.htgroup\n')
            fd.write('require group '+sample+'\n')
            fd.close()

        # grab table we want to display
        cursor.execute("DROP TABLE IF EXISTS hd;")
        cursor.execute("CREATE TEMPORARY TABLE hd SELECT sdbid,GROUP_CONCAT(xid) as xid"
                       " FROM sdb_pm LEFT JOIN xids USING (sdbid) WHERE xid REGEXP('^HD')"
                       " GROUP BY sdbid;")
        cursor.execute("DROP TABLE IF EXISTS hip;")
        cursor.execute("CREATE TEMPORARY TABLE hip SELECT sdbid,GROUP_CONCAT(xid) as xid"
                       " FROM sdb_pm LEFT JOIN xids USING (sdbid) WHERE xid REGEXP('^HIP')"
                       " GROUP BY sdbid;")
        cursor.execute("DROP TABLE IF EXISTS gj;")
        cursor.execute("CREATE TEMPORARY TABLE gj SELECT sdbid,GROUP_CONCAT(xid) as xid"
                       " FROM sdb_pm LEFT JOIN xids USING (sdbid) WHERE xid REGEXP('^GJ')"
                       " GROUP BY sdbid;")
        sel = ("SELECT "
               "CONCAT('<a href=\"../../seds/masters/',sdbid,'/public/',sdbid,'.png\">',sdbid,'</a>') as sdbid,"
               "CONCAT('<a href=\"http://simbad.u-strasbg.fr/simbad/sim-basic?submit=SIMBAD+search&Ident=',main_id,'\" target=\"_blank\">',main_id,'</a>') as Simbad,"
               "hd.xid as HD,"
               "hip.xid as HIP,"
               "gj.xid as GJ,"
               "concat('<a href=\"http://irsa.ipac.caltech.edu/applications/finderchart/#id=Hydra_finderchart_finder_chart&RequestClass=ServerRequest&DoSearch=true&subsize=0.08333333400000001&thumbnail_size=medium&sources=DSS,SDSS,twomass,WISE,IRIS&overlay_catalog=true&catalog_by_radius=true&iras_radius=240&sdss_radius=5&twomass_radius=5&wise_radius=5&one_to_one=_none_&dss_bands=poss1_blue,poss1_red,poss2ukstu_blue,poss2ukstu_red,poss2ukstu_ir&SDSS_bands=u,g,r,i,z&twomass_bands=j,h,k&wise_bands=1,2,3,4&UserTargetWorldPt=',raj2000,';',dej2000,';EQ_J2000&projectId=finderchart&searchName=finder_chart&shortDesc=Finder%20Chart&isBookmarkAble=true&isDrillDownRoot=true&isSearchResult=true\" target=\"_blank\">images</a>') as Finder,"
               "ROUND(-2.5*log10(ANY_VALUE(flux)/3882.37),1) as Vmag,"
               "raj2000 as RA,"
               "dej2000 as `Dec`,"
               "sp_type as SpType,"
               "ROUND(log10(lstar),2) as LogLstar,"
               "1e3/plx_value as Dist,"
               "ROUND(log10(ldisklstar),1) as Log_f")
            
        # here we decide which samples get all targets, for now "all" and "public" get
        # everything, but this could be changed so that "public" is some subset of "all"
        if sample == 'all' or sample == 'public':
            sel += " FROM simbad"
        else:
            sel += " FROM sed_db_samples."+sample+" LEFT JOIN simbad USING (sdbid)"
            
        sel += (" LEFT JOIN sdb_pm USING (sdbid)"
                " LEFT JOIN sed_stfit on sdbid=sed_stfit.name"
                " LEFT JOIN sed_bbfit on sdbid=sed_bbfit.name"
                " LEFT JOIN hd USING (sdbid)"
                " LEFT JOIN hip USING (sdbid)"
                " LEFT JOIN gj USING (sdbid)"
                " LEFT JOIN sed_photosphere on sdbid=sed_photosphere.name"
                " WHERE band='VJ'"
                " ORDER by RA;")
        cursor.execute(sel)
        tsamp = Table(names=cursor.column_names,
                      dtype=('S200','S200','S50','S50','S50','S1000','f','f','f','S10','f','f','f'))
        for row in cursor:
            tsamp.add_row(row)

        # write html page with interactive table, astropy 1.2.1 doesn't allow all of the
        # the htmldict contents to be passed to write_table_jsviewer so links are
        # bleached out. can use jsviewer with my modifications to that function...
        fd = open(wwwroot+sample+'/index.html','w')
#        tsamp.write(fd,format='ascii.html',htmldict={'raw_html_cols':['sdbid','Simbad','Finder'],
#                                                     'raw_html_clean_kwargs':{'attributes':{'a':['href','target']}} })
        jsviewer.write_table_jsviewer(tsamp,fd,max_lines=1000,table_id=sample,
                                      table_class="display compact",jskwargs={'display_length':100},
                                      raw_html_cols=['sdbid','Simbad','Finder'],
                                      raw_html_clean_kwargs={'attributes':{'a':['href','target']}} )
        fd.close()

    cnx.close()

# run from the command line
if __name__ == "__main__":

    # inputs
    parser1 = argparse.ArgumentParser(description='Update web pages')
    parser = parser1.add_mutually_exclusive_group(required=True)
    parser.add_argument('--samples','-s',action='store_true',help='Update sample pages')
    args = parser1.parse_args()

    if args.samples:
        print("Updating sample pages")
        sdb_www_samples()
