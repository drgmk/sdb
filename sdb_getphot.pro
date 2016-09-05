;+
;
; NAME:
;         SDB_GETPHOT
;
; PURPOSE:
;         Get photometry for an object in the sed-db
;
; CATEGORY:
;         SEDFitting
;
; EXPLANATION:
;
;         Use ids in the 'xid' table to collect photometry for a star, and write it to
;         and XDR save file for SEDFIT.
;
; SYNTAX:
;         sdb_getphot,id
;
; INPUTS:
;         id: ID or PPMXL number
;
; KEYWORD PARAMETERS:
;
;    VERBOSE: be verbose
;
; MODIFICATION HISTORY:
;         Written in late 2015, just updating from sedfit_dbget for updated sed-db
;
;-

;; use for checking whether a file is really new
function sdb_rawphot_chksum,FILE=file,RAWPHOT=rp
  if TOTAL(N_ELEMENTS(file)+N_ELEMENTS(rp)) ne 1 then STOP,'Dont give sdb_rawphot_chksum both or none'
  if N_ELEMENTS(file) ne 0 then restore,file else rawphot = rp
  checksum32,[rawphot.meas,rawphot.err,rawphot.syserr],s
  return,s
end

pro sdb_getphot,id1,location=loc,project=project,VERBOSE=verb

  if N_ELEMENTS(loc) eq 0 then loc = '~/astro/projects/sed-db/seds/masters/'

  ;; open database connection
  openmysql,lun,'sed_db'

  if KEYWORD_SET(project) then begin
     mysqlquery,lun,'SELECT sdbid FROM projects WHERE project="'+project+'";',id,V=KEYWORD_SET(verb),F='a',N=n
     for i=0,n-1 do sdb_getphot,id[i],VERBOSE=KEYWORD_SET(verb)
     return
  endif

  ;; set up temporary table with what we want
  mysqlcmd,lun,"CREATE TEMPORARY TABLE fluxes ( SourceID varchar(50) DEFAULT NULL,Band varchar(8) NOT NULL DEFAULT '',Phot double DEFAULT NULL,Err double DEFAULT NULL,SysErr double DEFAULT NULL,Lim int(1) NOT NULL DEFAULT '0',Ref varchar(30) NOT NULL DEFAULT '',Unit varchar(10) NOT NULL DEFAULT '',Note1 varchar(100) NOT NULL DEFAULT '',Note2 varchar(100) NOT NULL DEFAULT '');",DEBUG=KEYWORD_SET(verb)

  ;; get sdbid and xids
  mysqlquery,lun,'SELECT DISTINCT sdbid FROM xids WHERE xid="'+id1+'";',sdbid,VERBOSE=KEYWORD_SET(verb),format='a'
  mysqlquery,lun,'SELECT DISTINCT xid FROM xids WHERE sdbid="'+sdbid+'";',xids,VERBOSE=KEYWORD_SET(verb)
  print,'ID: ',id1,sdbid

  ;; make cross id table to match on
  mysqlcmd,lun,"DROP TABLE IF EXISTS TheIDs;"
  mysqlcmd,lun,'CREATE TEMPORARY TABLE TheIDs SELECT * from xids WHERE sdbid = "'+sdbid+'";',DEBUG=KEYWORD_SET(verb)
  mysqlcmd,lun,"ALTER TABLE TheIDs ADD INDEX (xid);"

  ;; now add photometry to that table
  mysqlquery,lun,"SELECT * FROM xmatch;",incl,table,xid,band,col,ecol,sys,lim,bib,unit,c1,c2,ex,FORMAT='i,a,a,a,a,a,a,a,a,a,a,a,a',NGOOD=n
  for i=0,n-1 do begin
     if incl[i] ne 1 then CONTINUE
     mysqlcmd,lun,"Insert INTO fluxes SELECT DISTINCT TheIDs.xid,"+band[i]+" AS band,"+col[i]+" AS flux,"+ecol[i]+" AS error,"+sys[i]+" AS syserror,"+lim[i]+" AS uplim,"+bib[i]+" AS ref,'"+unit[i]+"' AS unit,"+c1[i]+" as Note1,"+c2[i]+" AS Note2 FROM TheIDs LEFT JOIN "+table[i]+" ON TheIDs.xid="+xid[i]+" WHERE "+col[i]+" IS NOT NULL "+ex[i]+";",DEBUG=KEYWORD_SET(verb)
  endfor

  ;; now get it, need to get ascii and fix NULL values for floats
  mysqlquery,lun,"SELECT DISTINCT * FROM fluxes;",sqlid,band,phot,err,syserr,lim,ref,unit,note1,note2,NGOOD=n,VERBOSE=KEYWORD_SET(verb),FORMAT='(a,a,a,a,a,i,a,a,a,a)'

  ;; can't do anything if we found no photometry
  if n eq 0 then begin
     print,'No photometry for ',id1,sdbid
     free_lun,lun
     return
  endif

  ;; tidy NULL values and set uppper limits
  tmp = WHERE(phot eq 'NULL')
  if tmp[0] ne -1 then phot[tmp] = 0.0
  phot = FLOAT(phot)
  tmp = WHERE(err eq 'NULL')
  if tmp[0] ne -1 then err[tmp] = 0.0
  err = FLOAT(err)
  tmp = WHERE(syserr eq 'NULL')
  if tmp[0] ne -1 then syserr[tmp] = 0.0
  syserr = FLOAT(syserr)
  tmp = WHERE( lim ne 1 )
  if tmp[0] ne -1 then lim[tmp] = 0
  rawdata = 'from SQL'          ; just a placeholder really

  ;; make struct of photometry including IRS spectrum
  rawphot = {nphot:N_ELEMENTS(band),band:band,meas:phot,err:err,syserr:syserr,unit:unit,lim:lim,ref:ref,note1:note1,note2:note2,id:sqlid,irs:{ok:0,other:''}}

  ;; add IRS spectra
  rawphot = sedfit_addirs(rawphot,xdr,xdr,VERBOSE=KEYWORD_SET(verb))

  ;; get initial stellar params
  mysqlquery,lun,'SELECT sp_type,sp_bibcode,plx_value,plx_err,plx_bibcode from simbad WHERE sdbid="'+sdbid+'";',spty,spty_ref,plx,e_plx,plx_ref,VERBOSE=KEYWORD_SET(verb),format='a,a,a,a,a'
  free_lun,lun

  teff = !VALUES.F_NAN
  tefferr = !VALUES.F_NAN
  logg = !VALUES.F_NAN
  mh = !VALUES.F_NAN
  if spty eq 'NULL' or spty eq '' then spty = ''
  if spty_ref eq 'NULL' or spty_ref eq '' then spty_ref = ''
  if plx eq 'NULL' or plx eq '' then plx = !VALUES.F_NAN
  if e_plx eq 'NULL' or e_plx eq '' then e_plx = !VALUES.F_NAN

  stinf = {ntries:0,name:sdbid,xids:xids,spty:spty,spty_ref:spty_ref,plx:plx,eplx:e_plx,plx_ref:plx_ref,dist:FLOAT(1e3/plx),teff:FLOAT(teff),tefferr:tefferr,logg:FLOAT(logg),mh:FLOAT(mh),av:0.}
        
  ;; save file (using given name)
  fn = loc+sdbid+'/'+sdbid+'.xdr'
  if ~ FILE_TEST(loc+sdbid,/DIRECTORY) then begin ; if no dir then create
     FILE_MKDIR,loc+sdbid
  endif else begin
     if FILE_TEST(fn) then begin ; if file exists see if it needs updating
        if sdb_rawphot_chksum(rawphot=rawphot) ne sdb_rawphot_chksum(file=fn) then begin
           fs = FILE_SEARCH(loc+sdbid+'/*')
           FILE_DELETE,fs
        endif else begin
           print,'SDB_GETPHOT: no need for update ',fn
           return
        endelse
     endif
  endelse
  save,filename=fn,rawdata,rawphot,stinf
  print,'SDB_GETPHOT: saved file ',fn

  free_lun,lun

end
