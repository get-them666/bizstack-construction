import os
import csv
import requests
from typing import Optional, Dict, Any

def get_master_regional_config(city: str, county: str = "") -> dict:
    """
    Unified router covering the 7 core Hampton Roads cities plus 
    Williamsburg, Currituck, and Elizabeth City / Pasquotank County.
    """
    city_upper = (city or "").strip().upper()
    county_upper = (county or "").strip().upper()

    # === THE 7 CORE HAMPTON ROADS CITIES ===
    if city_upper == "CHESAPEAKE":
        return {
            "url": "https://gis.cityofchesapeake.net/arcgis/rest/services/Property/Parcel/MapServer/0/query",
            "where_field": "ADDRESS",
            "out_fields": "ADDRESS,SQFT,SHAPE.STArea()",
            "mapping": {"house": "SQFT", "land": "SHAPE.STArea()"}
        }
    elif city_upper in ["VIRGINIA BEACH", "VA BEACH"]:
        return {
            "url": "https://gis.vbgov.com/arcgis/rest/services/Parcel/Parcel/MapServer/0/query",
            "where_field": "SITE_ADDRESS",
            "out_fields": "SITE_ADDRESS,SHAPE.AREA,LEGAL_SQFT", 
            "mapping": {"house": "LEGAL_SQFT", "land": "SHAPE.AREA"}
        }
    elif city_upper == "NORFOLK":
        return {
            "url": "https://gis.norfolk.gov/arcgis/rest/services/Property/Parcels/MapServer/0/query",
            "where_field": "ADDRESS",
            "out_fields": "ADDRESS,SQFT,SHAPE.AREA",
            "mapping": {"house": "SQFT", "land": "SHAPE.AREA"}
        }
    elif city_upper == "NEWPORT NEWS":
        return {
            "url": "https://gis.nngov.com/arcgis/rest/services/Property/Parcels/MapServer/0/query",
            "where_field": "FULL_ADDRESS",
            "out_fields": "FULL_ADDRESS,LIV_AREA,Shape.STArea()",
            "mapping": {"house": "LIV_AREA", "land": "Shape.STArea()"}
        }
    elif city_upper == "HAMPTON":
        return {
            "url": "https://gis.hampton.gov/arcgis/rest/services/Property/Parcels/MapServer/0/query",
            "where_field": "ST_ADDR",
            "out_fields": "ST_ADDR,BLDG_SQFT,SHAPE.STArea()",
            "mapping": {"house": "BLDG_SQFT", "land": "Shape.STArea()"}
        }
    elif city_upper == "PORTSMOUTH":
        return {
            "url": "https://gis.portsmouthva.gov/arcgis/rest/services/Property/Parcels/MapServer/0/query",
            "where_field": "ADDRESS",
            "out_fields": "ADDRESS,SQ_FT,SHAPE_Area",
            "mapping": {"house": "SQ_FT", "land": "SHAPE_Area"}
        }
    elif city_upper == "SUFFOLK":
        return {
            "url": "https://gis.suffolkva.us/arcgis/rest/services/Property/Parcels/MapServer/0/query",
            "where_field": "SITUS_ADDRESS",
            "out_fields": "SITUS_ADDRESS,FIN_SQ_FT,SHAPE.AREA",
            "mapping": {"house": "FIN_SQ_FT", "land": "SHAPE.AREA"}
        }

    # === EXPANSION REGIONS ===
    elif city_upper == "WILLIAMSBURG":
        return {
            "url": "https://gis.williamsburgva.gov/arcgis/rest/services/Property/Parcels/MapServer/0/query",
            "where_field": "StreetAddress",
            "out_fields": "StreetAddress,FinishedSQFT,Shape.STArea()",
            "mapping": {"house": "FinishedSQFT", "land": "Shape.STArea()"}
        }
    elif "CURRITUCK" in county_upper or city_upper in ["MOYOCK", "CURRITUCK", "COROLLA", "POWELLS POINT"]:
        return {
            "url": "https://maps.currituckcountync.gov/arcgis/rest/services/TaxParcels/MapServer/0/query",
            "where_field": "LOC_STR",
            "out_fields": "LOC_STR,BLDG_VAL,ACREAGE_GI",
            "mapping": {"house": "BLDG_VAL", "land": "ACREAGE_GI", "is_acre": True}
        }
    elif city_upper == "ELIZABETH CITY" or "PASQUOTANK" in county_upper:
        return {
            "url": "https://gis.nconemap.gov/arcgis/rest/services/Pasquotank/Parcels/MapServer/0/query",
            "where_field": "SITE_ADDRE",
            "out_fields": "SITE_ADDRE,PARCEL_BRF,SHAPE.AREA",
            "mapping": {"house": "PARCEL_BRF", "land": "SHAPE.AREA"}
        }

    return None


def fetch_property_data(street_address: str, city: str, county: str = "", filename: str = "regional_property_database.csv") -> Optional[Dict[str, Any]]:
    """
    Queries any of the 10 defined localities, standardizes the returned data
    """
    config = get_master_regional_config(city, county)
    if not config:
        return None

    formatted_addr = (street_address or "").strip().upper()
    if not formatted_addr:
        return None
        
    params = {
        "where": f"{config['where_field']} LIKE '%{formatted_addr}%'",
        "outFields": config["out_fields"],
        "returnGeometry": "false",
        "f": "json"
    }

    try:
        response = requests.get(config["url"], params=params, timeout=10)
        data = response.json()
        
        if "features" in data and len(data["features"]) > 0:
            attributes = data["features"][0]["attributes"] 
            
            house_size = attributes.get(config["mapping"]["house"])
            land_size = attributes.get(config["mapping"]["land"])

            if config["mapping"].get("is_acre") and isinstance(land_size, (int, float)):
                land_size = round(land_size * 43560)
                if house_size and isinstance(house_size, (int, float)) and house_size > 10000:
                    house_size = None

            result = {
                "address": formatted_addr,
                "city": city.upper(),
                "county": county.upper(),
                "house_sqft": house_size,
                "lot_sqft": land_size,
                "source": "regional_gis"
            }
            
            try:
                file_exists = os.path.isfile(filename)
                with open(filename, mode='a', newline='', encoding='utf-8') as csv_file:
                    writer = csv.writer(csv_file)
                    if not file_exists:
                        writer.writerow(["Street Address", "City", "County", "House Sq Ft", "Lot Size Sq Ft"])
                    writer.writerow([formatted_addr, city.upper(), county.upper(), house_size if house_size is not None else "N/A", land_size if land_size is not None else "N/A"])
            except Exception:
                pass
                
            return result
        return None
    except Exception:
        return None


def lookup_property_regional(address: str) -> Optional[Dict[str, Any]]:
    """Try to lookup property using regional GIS based on address parsing"""
    if not address:
        return None
    addr_upper = address.upper()
    
    # Try to detect city
    cities = ["CHESAPEAKE", "VIRGINIA BEACH", "VA BEACH", "NORFOLK", "NEWPORT NEWS", "HAMPTON", "PORTSMOUTH", "SUFFOLK", "WILLIAMSBURG", "ELIZABETH CITY"]
    counties = ["CURRITUCK", "PASQUOTANK"]
    
    detected_city = None
    detected_county = None
    
    for c in cities:
        if c in addr_upper:
            detected_city = "VIRGINIA BEACH" if c == "VA BEACH" else c
            break
    for co in counties:
        if co in addr_upper:
            detected_county = co
            break
    
    if "MOYOCK" in addr_upper or "COROLLA" in addr_upper:
        detected_county = "CURRITUCK"
        detected_city = "MOYOCK" if "MOYOCK" in addr_upper else detected_city
    
    if detected_city or detected_county:
        # Try to extract street number and name
        parts = address.split(',')
        street_part = parts[0].strip()
        return fetch_property_data(street_part, detected_city or "", detected_county or "")
    
    return None
