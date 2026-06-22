python station_output_calculator_0p1deg.py \
    --csv data/stations/stations_SSP1-2.6.csv \
    --years 2030 2040 2050 \
    --source nam12 --gcm CanESM5 --realization r1i1p2f1 --rcm CRCM5 

python station_output_calculator_0p1deg.py \
    --csv data/stations/stations_SSP2-4.5.csv \
    --years 2030 2040 2050 \
    --source nam12 --gcm CanESM5 --realization r1i1p2f1 --rcm CRCM5 

python station_output_calculator_0p1deg.py \
    --csv data/stations/stations_SSP5-6.0.csv \
    --years 2030 2040 2050 \
    --source nam12 --gcm CanESM5 --realization r1i1p2f1 --rcm CRCM5 