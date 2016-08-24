;; populate xmatch lookup table with stuff from sedfit_dbget.pro

openmysql,lun,'sed_db'

cn = dbget_config(n=n)
for j=0,n-1 do begin
   c = cn[j,*]
   mysqlcmd,lun,'INSERT INTO xmatch (incl,`table`,xid,band,col,ecol,syserr,lim,bibcode,unit,com1,com2,extra) VALUES (0,"'+c[9]+'","'+c[10]+'","'+c[0]+'","'+c[1]+'","'+c[2]+'","'+c[3]+'","'+c[4]+'","'+c[5]+'","'+c[6]+'","'+c[7]+'","'+c[8]+'","'+c[11]+'");'
endfor

end
