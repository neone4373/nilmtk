from __future__ import print_function, division
import re
import os
import datetime
import sys
import pandas as pd
from pandas import HDFStore
import numpy as np
from collections import namedtuple
from nilmtk.dataset import DataSet
from nilmtk.building import Building
from nilmtk.measurement import Power

"""
MANUAL:

WikiEnergy is a large dataset hosted in a remote SQL database. This class provides 
a function to download the dataset and save it to disk as NILMTK-DF. Since downloading
the entire dataset will likely take > 24 hours, this class provides some options to allow
you to download only a subset of the data.

For example, to only load house 26 for April 2014:

wikienergy.download_dataset(
           'username',
           'password',
           '/path_to_output_directory/'
           periods_to_load = {26: ('2014-04-01', '2014-05-01')}
           )
"""

"""
TODO:
* intelligently handle queries that fail due to network
* fix issue on appending to an existing HDF5 store
"""

# Maps from UKPD name to:
#   tuple : ('<nilmtk name>', <metadata dict>)

# TODO: fill in this map
APPLIANCE_NAME_MAP = {
    #    'oven': ApplianceMetadata('oven', {'fuel':'electricity', 'dualsupply': True}),
}
    
def _wikienergy_dataframe_to_hdf(wikienergy_dataframe, hdf5_store):
    local_dataframe = wikienergy_dataframe.copy()
    local_dataframe = local_dataframe.set_index('localminute')
    
    for building_id in local_dataframe['dataid'].unique():
        feeds_dataframe = local_dataframe.drop('dataid', axis=1)
        feeds_dataframe = feeds_dataframe.mul(1000) # convert from kW to W
        meter_id = 1
        for column in feeds_dataframe.columns:
            if feeds_dataframe[column].notnull().sum() > 0:
                feed_dataframe = pd.DataFrame(feeds_dataframe[column])
                key = 'building{:d}/elec/meter{:d}'.format(building_id, meter_id)
                hdf5_store.append(key, feed_dataframe)
            meter_id = meter_id + 1
    return 0
    
class WikiEnergy(DataSet):

    """Load data from WikiEnergy."""

    def __init__(self):
        super(WikiEnergy, self).__init__()
        self.metadata = {
            'name': 'WikiEnergy'
        }
    
    def download_dataset(self, database_username, database_password, output_directory, periods_to_load=None):
        """
        Parameters
        ----------
        periods_to_load : dict of tuples, optional
           Key of dict is the building number (int).
           Values are (<start date>, <end date>)
           e.g. ("2013-04-01", None) or ("2013-04-01", "2013-08-01")
           defaults to all buildings and all date ranges
        """
        
        import psycopg2 as db
        
        # wiki-energy database settings
        database_host = 'db.wiki-energy.org'
        database_name = 'postgres'
        database_schema = 'PecanStreet_SharedData'
        
        # set up a new HDF5 datastore
        full_directory = output_directory + 'wikienergy.h5'
        if os.path.isfile(full_directory):
            os.remove(full_directory)
        hdf5_store = HDFStore(full_directory)
        
        # try to connect to database
        try:
            conn = db.connect('host=' + database_host + 
                                           ' dbname=' + database_name + 
                                           ' user=' + database_username + 
                                           ' password=' + database_password)
        except:
            print('Could not connect to remote database')
            return
        
        # get tables in database schema
        sql_query = ('SELECT TABLE_NAME' + 
                     ' FROM INFORMATION_SCHEMA.TABLES' + 
                     " WHERE TABLE_TYPE = 'BASE TABLE'" + 
                     " AND TABLE_SCHEMA='" + database_schema + "'" + 
                     " ORDER BY TABLE_NAME")
        database_tables = pd.read_sql(sql_query, conn)['table_name'].tolist()
        
        # if user has specified buildings
        if periods_to_load:
            buildings_to_load = periods_to_load.keys()
        else:
            # get buildings present in all tables
            sql_query = ''
            for table in database_tables:
                sql_query = (sql_query + '(SELECT DISTINCT dataid' + 
                             ' FROM "' + database_schema + '".' + table + 
                             ') UNION ')
            sql_query = sql_query[:-7]
            sql_query = (sql_query + ' ORDER BY dataid') 
            buildings_to_load = pd.read_sql(sql_query, conn)['dataid'].tolist()
        
        # for each user specified building or all buildings in database
        for building_id in buildings_to_load:
            print("Loading building {:d}".format(building_id) + ' @ ' + str(datetime.datetime.now()))
            sys.stdout.flush()
            
            # create new list of chunks for csv writing
            dataframe_list = []
        
            # for each table of 1 month data
            for database_table in database_tables:
                print("  Loading table {:s}".format(database_table))
                sys.stdout.flush()
            
                # get buildings present in this table
                sql_query = ('SELECT DISTINCT dataid' + 
                             ' FROM "' + database_schema + '".' + database_table + 
                             ' ORDER BY dataid')
                buildings_in_table = pd.read_sql(sql_query, conn)['dataid'].tolist()
            
                if building_id in buildings_in_table:
                    # get first and last timestamps for this house in this table
                    sql_query = ('SELECT MIN(localminute) AS minlocalminute,' + 
                                 ' MAX(localminute) AS maxlocalminute' + 
                                 ' FROM "' + database_schema + '".' + database_table + 
                                 ' WHERE dataid=' + str(building_id))
                    range = pd.read_sql(sql_query, conn)
                    first_timestamp_in_table = range['minlocalminute'][0]
                    last_timestamp_in_table = range['maxlocalminute'][0]
                    
                    # get requested start and end and localize them
                    requested_start = None
                    requested_end = None
                    database_timezone = 'US/Central'
                    if periods_to_load:
                        if periods_to_load[building_id][0]:
                            requested_start = pd.Timestamp(periods_to_load[building_id][0])
                            requested_start = requested_start.tz_localize(database_timezone)
                        if periods_to_load[building_id][1]:
                            requested_end = pd.Timestamp(periods_to_load[building_id][1])
                            requested_end = requested_end.tz_localize(database_timezone)
                    
                    # check user start is not after end
                    if requested_start > requested_end:
                        print('requested end is before requested start')
                        sys.stdout.flush()
                    else:                        
                        # clip data to smallest range
                        if requested_start:
                            start = max(requested_start, first_timestamp_in_table)
                        else:
                            start = first_timestamp_in_table
                        if requested_end:
                            end = min(requested_end, last_timestamp_in_table)
                        else:
                            end = last_timestamp_in_table
                        
                        # download data in chunks
                        chunk_start = start
                        chunk_size = datetime.timedelta(1)  # 1 day
                        while chunk_start < end:
                            chunk_end = chunk_start + chunk_size 
                            if chunk_end > end:
                                chunk_end = end
                            # subtract 1 second so end is exclusive
                            chunk_end = chunk_end - datetime.timedelta(0, 1)
                            
                            # query power data for all channels
                            format = '%Y-%m-%d %H:%M:%S'
                            sql_query = ('SELECT *' + 
                                         ' FROM "' + database_schema + '".' + database_table + 
                                         ' WHERE dataid=' + str(building_id) + 
                                         'and localminute between ' + 
                                         "'" + chunk_start.strftime(format) + "'" + 
                                         " and " + 
                                         "'" + chunk_end.strftime(format) + "'" + 
                                         ' LIMIT 2000')
                            chunk_dataframe = pd.read_sql(sql_query, conn)
                            
                            # convert to nilmtk-df and save to disk
                            nilmtk_dataframe = _wikienergy_dataframe_to_hdf(chunk_dataframe, hdf5_store)
                            
                            # print progress
                            print('    ' + str(chunk_start) + ' -> ' + 
                                  str(chunk_end) + ': ' + 
                                  str(len(chunk_dataframe.index)) + ' rows')
                            sys.stdout.flush()
                            
                            # append all chunks into list for csv writing
                            dataframe_list.append(chunk_dataframe)
                            
                            # move on to next chunk
                            chunk_start = chunk_start + chunk_size
                        
            # saves all chunks in list to csv
            if len(dataframe_list) > 0:
                dataframe_concat = pd.concat(dataframe_list)
                #dataframe_concat.to_csv(output_directory + str(building_id) + '.csv')
                
        hdf5_store.close()
        conn.close()
        
        