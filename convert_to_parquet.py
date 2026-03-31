import geopandas as gpd

shp = gpd.read_file("C:\\Users\\krist\\Documents\\ArcGIS\\Projects\\Jorbruksverket\\Output\\Subparcels_Dalarna.shp")
shp = shp.to_crs("EPSG:32633")  # UTM Zone 33N
shp.to_parquet("C:\\Users\\krist\\code\\Repos\\thesis\\data\\subparcels\\subparcels_dalarna.parquet")