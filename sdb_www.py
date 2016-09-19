#!/Users/grant/.virtualenvs/astropy-dev/bin/python3
#!/usr/bin/env python3

"""Generate HTML pages to browse database

   Uses non-standard version of astropy to ensure html anchors in jsviewer tables

   """

import numpy as np
from bokeh.plotting import figure,output_file,save,ColumnDataSource
import bokeh.palettes
from bokeh.models import HoverTool
from bokeh.layouts import gridplot,layout
from os.path import isdir,isfile
from os import mkdir,remove,write
from astropy.table import Table,jsviewer
import argparse
import mysql.connector
import config as cfg

def sdb_www_get_samples():
    """Get a list of samples
    
    Get a list of samples from the database. Add "everything" and "public"
    samples, "public" might not show everything in the list, but
    "everything" will (but may not be visible to anyone).
    """
    
    cnx = mysql.connector.connect(user=cfg.mysql['user'],password=cfg.mysql['passwd'],
                                  host=cfg.mysql['host'],database=cfg.mysql['db'])
    cursor = cnx.cursor(buffered=True)
    cursor.execute("SELECT DISTINCT project FROM projects;")
    samples = cursor.fetchall() # a list of tuples
    samples = [i[0] for i in samples]
    cursor.close()
    cnx.close()
    return( samples + ['public','everything'] )

def sdb_www_sample_tables():
    """Generate HTML pages with sample tables

    Extract the necessary information from the database and create HTML
    pages with the desired tables, one for each sample. These are generated
    using astropy's HTML table writer and the jsviewer,which makes tables
    that are searchable and sortable.
    """

    wwwroot = cfg.www['root']+'samples/'

    # set up connection
    cnx = mysql.connector.connect(user=cfg.mysql['user'],password=cfg.mysql['passwd'],
                                  host=cfg.mysql['host'],database=cfg.mysql['db'])
    cursor = cnx.cursor(buffered=True)

    # get a list of samples and generate their pages
    samples = sdb_www_get_samples()
    for sample in samples:

        print("    sample:",sample)

        # make dir and .htaccess if dir doesn't exist
        if not isdir(wwwroot+sample):
            mkdir(wwwroot+sample)

        # make .htaccess if needed, don't put one in "public" or those ending with "_"
        # so stuff in those directories remains visible to those not logged in
        if  sample[-1] != '_' and sample != 'public':
            fd = open(wwwroot+sample+'/.htaccess','w')
            fd.write('AuthName "Must login"\n')
            fd.write('AuthType Basic\n')
            fd.write('AuthUserFile '+cfg.www['root']+'.htpasswd\n')
            fd.write('AuthGroupFile '+cfg.www['root']+'.htgroup\n')
            fd.write('require group admin '+sample+'\n')
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
        cursor.execute("DROP TABLE IF EXISTS phot;")
        cursor.execute("CREATE TEMPORARY TABLE phot SELECT"
                       " name as sdbid,ROUND(-2.5*log10(ANY_VALUE(flux)/3882.37),1) as Vmag"
                       " FROM sed_photosphere WHERE band='VJ' GROUP BY sdbid;")
        sel = ("SELECT "
               "CONCAT('<a href=\"../../seds/masters/',sdbid,'/public/',sdbid,'.png\">',sdbid,'</a>') as sdbid,"
               "CONCAT('<a href=\"http://simbad.u-strasbg.fr/simbad/sim-basic?submit=SIMBAD+search&Ident=',main_id,'\" target=\"_blank\">',main_id,'</a>') as Simbad,"
               "hd.xid as HD,"
               "hip.xid as HIP,"
               "gj.xid as GJ,"
               "concat('<a href=\"http://irsa.ipac.caltech.edu/applications/finderchart/#id=Hydra_finderchart_finder_chart&RequestClass=ServerRequest&DoSearch=true&subsize=0.08333333400000001&thumbnail_size=medium&sources=DSS,SDSS,twomass,WISE,IRIS&overlay_catalog=true&catalog_by_radius=true&iras_radius=240&sdss_radius=5&twomass_radius=5&wise_radius=5&one_to_one=_none_&dss_bands=poss1_blue,poss1_red,poss2ukstu_blue,poss2ukstu_red,poss2ukstu_ir&SDSS_bands=u,g,r,i,z&twomass_bands=j,h,k&wise_bands=1,2,3,4&UserTargetWorldPt=',raj2000,';',dej2000,';EQ_J2000&projectId=finderchart&searchName=finder_chart&shortDesc=Finder%20Chart&isBookmarkAble=true&isDrillDownRoot=true&isSearchResult=true\" target=\"_blank\">images</a>') as Finder,"
               "Vmag,"
               "raj2000 as RA,"
               "dej2000 as `Dec`,"
               "sp_type as SpType,"
               "teff as Teff,"
               "ROUND(log10(lstar),2) as LogLstar,"
               "1e3/COALESCE(tgas.plx,simbad.plx_value) AS Dist,"
               "ROUND(log10(ldisklstar),1) as Log_f,"
               "tdisk_cold as T_disk")
            
        # here we decide which samples get all targets, for now "everything" and "public"
        # get everything, but this could be changed so that "public" is some subset of
        # "everything"
        if sample == 'everything' or sample == 'public':
            sel += " FROM sdb_pm"
        else:
            sel += " FROM "+cfg.mysql['sampledb']+"."+sample+" LEFT JOIN sdb_pm USING (sdbid)"
            
        sel += (" LEFT JOIN simbad USING (sdbid)"
                " LEFT JOIN tyc2 USING (sdbid)"
                " LEFT JOIN photometry.tgas ON COALESCE(-tyc2.hip,tyc2.tyc2id)=tgas.tyc2hip"
                " LEFT JOIN sed_stfit on sdbid=sed_stfit.name"
                " LEFT JOIN sed_bbfit on sdbid=sed_bbfit.name"
                " LEFT JOIN hd USING (sdbid)"
                " LEFT JOIN hip USING (sdbid)"
                " LEFT JOIN gj USING (sdbid)"
                " LEFT JOIN phot USING (sdbid)"
                " ORDER by RA")
        # limit table sizes
        if sample != 'everything':
            sel += " LIMIT "+str(cfg.www['tablemax'])+";"

#        print(sel)
        cursor.execute(sel)
        tsamp = Table(names=cursor.column_names,
                      dtype=('S200','S200','S50','S50','S50','S1000','f','f','f','S10','f','f','f','f','f'))
        for row in cursor:
            tsamp.add_row(row)
        print("    got ",len(tsamp)," rows")

        # write html page with interactive table, astropy 1.2.1 doesn't allow all of the
        # the htmldict contents to be passed to write_table_jsviewer so links are
        # bleached out. can use jsviewer with my modifications to that function...
        fd = open(wwwroot+sample+'/table.html','w')
#        tsamp.write(fd,format='ascii.html',htmldict={'raw_html_cols':['sdbid','Simbad','Finder'],
#                                                     'raw_html_clean_kwargs':{'attributes':{'a':['href','target']}} })
        jsviewer.write_table_jsviewer(tsamp,fd,max_lines=10000,table_id=sample,
                                      table_class="display compact",jskwargs={'display_length':100},
                                      raw_html_cols=['sdbid','Simbad','Finder'],
                                      raw_html_clean_kwargs={'attributes':{'a':['href','target']}} )
        fd.close()

    cursor.close()
    cnx.close()

def sdb_www_sample_plots():
    """Generate HTML pages with sample plots

    Extract the necessary information from the database and plot using bokeh.
    """

    wwwroot = cfg.www['root']+'samples/'

    # set up connection
    cnx = mysql.connector.connect(user=cfg.mysql['user'],password=cfg.mysql['passwd'],
                                  host=cfg.mysql['host'],database=cfg.mysql['db'])
    cursor = cnx.cursor(buffered=True)

    # get a list of samples and generate their pages
    samples = sdb_www_get_samples()
    for sample in samples:

        print("    sample:",sample)

        # get data, ensure primary axes are not nans
        selall = "SELECT sdbid,main_id,teff,lstar,ldisklstar,tdisk_cold"
        selnum = "SELECT COUNT(*)"
        if sample == 'everything' or sample == 'public':
            selall += " FROM sdb_pm"
            selnum += " FROM sdb_pm"
        else:
            selall += " FROM "+cfg.mysql['sampledb']+"."+sample+" LEFT JOIN sdb_pm USING (sdbid)"
            selnum += " FROM "+cfg.mysql['sampledb']+"."+sample+" LEFT JOIN sdb_pm USING (sdbid)"
        selall += (" LEFT JOIN simbad USING (sdbid)"
                   " LEFT JOIN sed_stfit ON sdbid=sed_stfit.name"
                   " LEFT JOIN sed_bbfit ON sdbid=sed_bbfit.name")
        selnum += (" LEFT JOIN simbad USING (sdbid)"
                   " LEFT JOIN sed_stfit ON sdbid=sed_stfit.name"
                   " LEFT JOIN sed_bbfit ON sdbid=sed_bbfit.name")
        selall += " WHERE teff IS NOT NULL AND lstar IS NOT NULL"
        # limit table sizes
        if sample != 'everything':
            selall += " LIMIT "+str(cfg.www['tablemax'])+";"
        cursor.execute(selnum)
        ntot = cursor.fetchall()[0][0]
        cursor.execute(selall)
        t = {}
        allsql = cursor.fetchall()
        ngot = len(allsql)
        print("    got ",ngot," rows")
        l = list(zip(*allsql))
        keys = cursor.column_names
        dtypes = [None,None,float,float,float,float]
        for i in range(len(keys)):
            col = np.array(l[i],dtype=dtypes[i])
            # check if all of a numeric col (i.e. one we will plot) are nan
            if dtypes[i] == float or dtypes[i] == int:
                if np.any(np.isfinite(col)) == False:
                    continue
            t[keys[i]] = col
        data = ColumnDataSource(data=t)   
       
        # remove the plot file to avoid overwrite warnings
        plfile = wwwroot+sample+"/hr.html"
        if isfile(plfile):
            remove(plfile)
        output_file(plfile,mode='cdn')

        # set up colour scale, grey for nans
        if 'ldisklstar' in t:
            cr = np.array([np.nanmin(np.log(t['ldisklstar'])),np.nanmax(np.log(t['ldisklstar']))])
            ci = 0.999*(np.log(t['ldisklstar'])-cr[0])/(cr[1]-cr[0]) # ensure top in below 1 for indexing
            ok = np.isfinite(ci)
            col = np.empty(ngot,dtype='U7')
            col[ok] = np.array(bokeh.palettes.plasma(100))[np.floor(100*ci[ok]).astype(int)]
            col[col==''] = '#969696'
        else:
            col = '#969696'

        # TODO: hover in one highlights in the other
        hover1 = HoverTool(tooltips=[("name","@main_id")])
        hover2 = HoverTool(tooltips=[("name","@main_id")])
        tools1 = ['wheel_zoom,box_zoom,box_select,tap,save,reset',hover1]
        tools2 = ['wheel_zoom,box_zoom,box_select,tap,save,reset',hover2]

        # hr diagram
        hr = figure(title="diagrams for "+sample+" ("+str(ngot)+" of "+str(ntot)+")",
                    tools=tools1,active_scroll='wheel_zoom',
                    x_axis_label='Effective temperature / K',y_axis_label='Stellar luminosity / Solar',
                    y_axis_type="log",y_range=(0.5*min(t['lstar']),max(t['lstar'])*2),
                    x_range=(300+max(t['teff']),min(t['teff'])-300) )
        hr.circle('teff','lstar',source=data,size=10,fill_color=col,
                  fill_alpha=0.6,line_color=None)

        # f vs temp (if we have any)
        if 'tdisk_cold' in t and 'ldisklstar' in t:
            ft = figure(tools=tools2,active_scroll='wheel_zoom',
                        x_axis_label='Disk temperature / K',y_axis_label='Disk fractional luminosity',
                        y_axis_type="log",y_range=(0.5*min(t['ldisklstar']),max(t['ldisklstar'])*2),
                        x_axis_type="log",x_range=(0.5*min(t['tdisk_cold']),max(t['tdisk_cold'])*2) )
            ft.circle('tdisk_cold','ldisklstar',source=data,size=10,fill_color=col,
                      fill_alpha=0.6,line_color=None)
        else:
            ft = figure(title='no IR excesses')
                
        p = gridplot([[hr,ft]],sizing_mode='stretch_both',
                     toolbar_location='above')
        save(p)

    cursor.close()
    cnx.close()

            
# run from the command line
if __name__ == "__main__":

    # inputs
    parser = argparse.ArgumentParser(description='Update web pages')
    parser.add_argument('--tables','-t',action='store_true',help='Update sample tables')
    parser.add_argument('--plots','-p',action='store_true',help='Update sample plots')
    args = parser.parse_args()

    if args.tables:
        print("Updating sample tables")
        sdb_www_sample_tables()

    if args.plots:
        print("Updating sample plots")
        sdb_www_sample_plots()

