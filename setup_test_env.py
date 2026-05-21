import asyncio
import asyncpg
import logging
import os
from sqlalchemy.ext.asyncio import create_async_engine
import alembic.config
import alembic.command

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def setup():
    logger.info("Connecting to Neon to create TS_TOURS_TEST database...")
    sys_conn = await asyncpg.connect('postgresql://neondb_owner:npg_pr3dCmWeuV0O@ep-dark-credit-aoskzmbc.c-2.ap-southeast-1.aws.neon.tech/neondb')
    
    try:
        # Check if exists
        exists = await sys_conn.fetchval("SELECT 1 FROM pg_database WHERE datname = 'TS_TOURS_TEST'")
        if exists:
            logger.info("Test database already exists. Dropping it for a clean slate...")
            await sys_conn.execute('DROP DATABASE "TS_TOURS_TEST" WITH (FORCE)')
            
        await sys_conn.execute('CREATE DATABASE "TS_TOURS_TEST"')
        logger.info("TS_TOURS_TEST database created successfully.")
    except Exception as e:
        logger.error(f"Failed to create database: {e}")
    finally:
        await sys_conn.close()

if __name__ == "__main__":
    asyncio.run(setup())
