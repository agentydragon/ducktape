# To use, run gnuplot and then 'load "plot-net-worth.gnuplot"'

set datafile separator ','
set xdata time
set format x '%Y-%m'
set timefmt '%Y-%m-%dT%H:%M:%SZ'
set nokey
set autoscale xfixmin
plot '/home/agentydragon/drive/finance/worthy.csv' u 1:2 w filledcurves x1
