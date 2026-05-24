#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
和风天气 Python SDK

Author: binglang
Version: 2.5.0
License: MIT
"""

import requests
import json
from typing import Optional, Dict, Any, List, Union, Tuple
from datetime import datetime, date
from enum import Enum
import gzip
from dataclasses import dataclass, field
from urllib.parse import urlencode
import re
import logging
from functools import lru_cache
import os
import time
import hashlib
import pickle
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


# 配置日志
logger = logging.getLogger(__name__)


# ========== 枚举类==========

class Unit(Enum):
    """单位类型枚举"""
    METRIC = "m"     # 公制单位（默认）
    IMPERIAL = "i"   # 英制单位
    
    # 别名
    M = "m"
    I = "i"


class Lang(Enum):
    """支持的语言枚举"""
    ZH = "zh"              # 简体中文（默认）
    ZH_HANT = "zh-hant"    # 繁体中文  
    EN = "en"              # 英语
    DE = "de"              # 德语
    ES = "es"              # 西班牙语
    FR = "fr"              # 法语
    IT = "it"              # 意大利语
    JA = "ja"              # 日语
    KO = "ko"              # 韩语
    RU = "ru"              # 俄语
    HI = "hi"              # 印地语
    TH = "th"              # 泰语
    AR = "ar"              # 阿拉伯语
    PT = "pt"              # 葡萄牙语
    BN = "bn"              # 孟加拉语
    MS = "ms"              # 马来语
    NL = "nl"              # 荷兰语
    EL = "el"              # 希腊语
    LA = "la"              # 拉丁语
    SV = "sv"              # 瑞典语
    ID = "id"              # 印尼语
    PL = "pl"              # 波兰语
    TR = "tr"              # 土耳其语
    CS = "cs"              # 捷克语
    ET = "et"              # 爱沙尼亚语
    VI = "vi"              # 越南语
    FI = "fi"              # 芬兰语
    
    # 常用别名
    CHINESE = "zh"
    CHINESE_TRADITIONAL = "zh-hant"
    ENGLISH = "en"
    JAPANESE = "ja"
    KOREAN = "ko"
    

class IndicesType(Enum):
    """天气生活指数类型枚举"""
    ALL = "0"    # 全部天气指数
    SPT = "1"    # 运动指数
    CW = "2"     # 洗车指数
    DRSG = "3"   # 穿衣指数
    FIS = "4"    # 钓鱼指数
    UV = "5"     # 紫外线指数
    TRA = "6"    # 旅游指数
    AG = "7"     # 花粉过敏指数
    COMF = "8"   # 舒适度指数
    FLU = "9"    # 感冒指数
    AP = "10"    # 空气污染扩散条件指数
    AC = "11"    # 空调开启指数
    GL = "12"    # 太阳镜指数
    MU = "13"    # 化妆指数
    DC = "14"    # 晾晒指数
    PTFC = "15"  # 交通指数
    SPI = "16"   # 防晒指数
    
    # 友好别名
    SPORT = "1"
    CAR_WASH = "2"
    DRESSING = "3"
    FISHING = "4"
    ULTRAVIOLET = "5"
    TRAVEL = "6"
    ALLERGY = "7"
    COMFORT = "8"
    COLD = "9"
    AIR_POLLUTION_SPREAD = "10"
    AIR_CONDITIONER = "11"
    SUNGLASSES = "12"
    MAKEUP = "13"
    DRYING = "14"
    TRAFFIC = "15"
    SUNSCREEN = "16"


# ========== 数据模型==========

@dataclass
class Location:
    """位置信息数据模型"""
    name: str
    id: str
    lat: float
    lon: float
    adm2: str = ""
    adm1: str = ""
    country: str = ""
    tz: str = ""
    utcOffset: str = ""
    isDst: str = "0"
    type: str = ""
    rank: str = ""
    fxLink: str = ""
    
    # 友好属性名（作为属性访问器）
    @property
    def latitude(self) -> float:
        return self.lat
        
    @property
    def longitude(self) -> float:
        return self.lon
        
    @property
    def city(self) -> str:
        return self.adm2
        
    @property
    def province(self) -> str:
        return self.adm1
        
    @property
    def timezone(self) -> str:
        return self.tz
        
    @property
    def is_dst(self) -> bool:
        return self.isDst == "1"
        
    @property
    def location_type(self) -> str:
        return self.type
        
    @property
    def coordinates(self) -> Tuple[float, float]:
        """获取坐标元组 (纬度, 经度)"""
        return (self.lat, self.lon)
    
    @property
    def coordinates_string(self) -> str:
        """获取坐标字符串 "经度,纬度" """
        return f"{self.lon},{self.lat}"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Location':
        """从字典创建Location对象"""
        return cls(
            name=data.get('name', ''),
            id=data.get('id', ''),
            lat=float(data.get('lat', 0)),
            lon=float(data.get('lon', 0)),
            adm2=data.get('adm2', ''),
            adm1=data.get('adm1', ''),
            country=data.get('country', ''),
            tz=data.get('tz', ''),
            utcOffset=data.get('utcOffset', ''),
            isDst=data.get('isDst', '0'),
            type=data.get('type', ''),
            rank=data.get('rank', ''),
            fxLink=data.get('fxLink', '')
        )


@dataclass
class WeatherNow:
    """实时天气数据模型"""
    obsTime: datetime
    temp: float
    feelsLike: float
    icon: str
    text: str
    wind360: int
    windDir: str
    windScale: str
    windSpeed: float
    humidity: int
    precip: float
    pressure: int
    vis: int
    cloud: Optional[int] = None
    dew: Optional[float] = None
    
    # 友好属性名
    @property
    def observation_time(self) -> datetime:
        return self.obsTime
        
    @property
    def temperature(self) -> float:
        return self.temp
        
    @property
    def feels_like(self) -> float:
        return self.feelsLike
        
    @property
    def weather_text(self) -> str:
        return self.text
        
    @property
    def wind_direction(self) -> str:
        return self.windDir
        
    @property
    def wind_direction_360(self) -> int:
        return self.wind360
        
    @property
    def wind_speed(self) -> float:
        return self.windSpeed
        
    @property
    def wind_scale(self) -> str:
        return self.windScale
        
    @property
    def precipitation(self) -> float:
        return self.precip
        
    @property
    def visibility(self) -> int:
        return self.vis
        
    @property
    def cloud_cover(self) -> Optional[int]:
        return self.cloud
        
    @property
    def dew_point(self) -> Optional[float]:
        return self.dew
    
    # 便捷方法
    @property
    def is_raining(self) -> bool:
        """是否在下雨"""
        return self.precip > 0
    
    @property
    def is_good_weather(self) -> bool:
        """是否是好天气"""
        good_weather_codes = ["100", "101", "102", "103", "150", "151", "152", "153"]
        return self.icon in good_weather_codes
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WeatherNow':
        """从字典创建WeatherNow对象"""
        obs_time_str = data.get('obsTime', '')
        obs_time_str = obs_time_str.replace('+08:00', '+0800')
        
        return cls(
            obsTime=datetime.fromisoformat(obs_time_str) if obs_time_str else datetime.now(),
            temp=float(data.get('temp', 0)),
            feelsLike=float(data.get('feelsLike', 0)),
            icon=data.get('icon', ''),
            text=data.get('text', ''),
            wind360=int(data.get('wind360', 0)),
            windDir=data.get('windDir', ''),
            windScale=data.get('windScale', ''),
            windSpeed=float(data.get('windSpeed', 0)),
            humidity=int(data.get('humidity', 0)),
            precip=float(data.get('precip', 0)),
            pressure=int(data.get('pressure', 0)),
            vis=int(data.get('vis', 0)),
            cloud=int(data.get('cloud')) if data.get('cloud') else None,
            dew=float(data.get('dew')) if data.get('dew') else None
        )


@dataclass
class DailyForecast:
    """每日天气预报数据模型"""
    fxDate: str
    sunrise: str
    sunset: str
    moonrise: str
    moonset: str
    moonPhase: str
    moonPhaseIcon: str
    tempMax: float
    tempMin: float
    iconDay: str
    textDay: str
    iconNight: str
    textNight: str
    wind360Day: int
    windDirDay: str
    windScaleDay: str
    windSpeedDay: float
    wind360Night: int
    windDirNight: str
    windScaleNight: str
    windSpeedNight: float
    humidity: int
    precip: float
    pressure: int
    vis: int
    cloud: Optional[int] = None
    uvIndex: Optional[int] = None
    
    # 友好属性名
    @property
    def date(self) -> date:
        return datetime.strptime(self.fxDate, '%Y-%m-%d').date()
        
    @property
    def forecast_date(self) -> str:
        return self.fxDate
        
    @property
    def moon_phase(self) -> str:
        return self.moonPhase
        
    @property
    def moon_phase_icon(self) -> str:
        return self.moonPhaseIcon
        
    @property
    def temp_max(self) -> float:
        return self.tempMax
        
    @property
    def temp_min(self) -> float:
        return self.tempMin
        
    @property
    def temperature_range(self) -> str:
        """温度范围字符串"""
        return f"{self.tempMin}~{self.tempMax}°"
        
    @property
    def day_weather(self) -> str:
        return self.textDay
        
    @property
    def night_weather(self) -> str:
        return self.textNight
        
    @property
    def day_icon(self) -> str:
        return self.iconDay
        
    @property
    def night_icon(self) -> str:
        return self.iconNight
        
    @property
    def precipitation(self) -> float:
        return self.precip
        
    @property
    def visibility(self) -> int:
        return self.vis
        
    @property
    def uv_index(self) -> Optional[int]:
        return self.uvIndex
    
    # 便捷方法
    @property
    def is_rainy(self) -> bool:
        """是否有雨"""
        rain_codes = ["300", "301", "302", "303", "304", "305", "306", "307", 
                     "308", "309", "310", "311", "312", "313", "314", "315", 
                     "316", "317", "318", "350", "351", "399"]
        return self.iconDay in rain_codes or self.iconNight in rain_codes
    
    @property
    def is_sunny(self) -> bool:
        """是否晴天"""
        sunny_codes = ["100", "150"]
        return self.iconDay in sunny_codes
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DailyForecast':
        """从字典创建DailyForecast对象"""
        return cls(
            fxDate=data.get('fxDate', ''),
            sunrise=data.get('sunrise', ''),
            sunset=data.get('sunset', ''),
            moonrise=data.get('moonrise', ''),
            moonset=data.get('moonset', ''),
            moonPhase=data.get('moonPhase', ''),
            moonPhaseIcon=data.get('moonPhaseIcon', ''),
            tempMax=float(data.get('tempMax', 0)),
            tempMin=float(data.get('tempMin', 0)),
            iconDay=data.get('iconDay', ''),
            textDay=data.get('textDay', ''),
            iconNight=data.get('iconNight', ''),
            textNight=data.get('textNight', ''),
            wind360Day=int(data.get('wind360Day', 0)),
            windDirDay=data.get('windDirDay', ''),
            windScaleDay=data.get('windScaleDay', ''),
            windSpeedDay=float(data.get('windSpeedDay', 0)),
            wind360Night=int(data.get('wind360Night', 0)),
            windDirNight=data.get('windDirNight', ''),
            windScaleNight=data.get('windScaleNight', ''),
            windSpeedNight=float(data.get('windSpeedNight', 0)),
            humidity=int(data.get('humidity', 0)),
            precip=float(data.get('precip', 0)),
            pressure=int(data.get('pressure', 0)),
            vis=int(data.get('vis', 0)),
            cloud=int(data.get('cloud')) if data.get('cloud') else None,
            uvIndex=int(data.get('uvIndex')) if data.get('uvIndex') else None
        )


@dataclass
class HourlyForecast:
    """逐小时天气预报数据模型"""
    fxTime: datetime
    temp: float
    icon: str
    text: str
    wind360: int
    windDir: str
    windScale: str
    windSpeed: float
    humidity: int
    precip: float
    pop: Optional[int]
    pressure: int
    cloud: Optional[int]
    dew: Optional[float]
    
    # 友好属性名
    @property
    def time(self) -> datetime:
        return self.fxTime
        
    @property
    def forecast_time(self) -> datetime:
        return self.fxTime
        
    @property
    def temperature(self) -> float:
        return self.temp
        
    @property
    def weather_text(self) -> str:
        return self.text
        
    @property
    def wind_direction(self) -> str:
        return self.windDir
        
    @property
    def wind_direction_360(self) -> int:
        return self.wind360
        
    @property
    def wind_speed(self) -> float:
        return self.windSpeed
        
    @property
    def wind_scale(self) -> str:
        return self.windScale
        
    @property
    def precipitation(self) -> float:
        return self.precip
        
    @property
    def precipitation_probability(self) -> Optional[int]:
        return self.pop
        
    @property
    def cloud_cover(self) -> Optional[int]:
        return self.cloud
        
    @property
    def dew_point(self) -> Optional[float]:
        return self.dew
    
    # 便捷方法
    @property
    def hour(self) -> int:
        """获取小时"""
        return self.fxTime.hour
    
    @property
    def is_daytime(self) -> bool:
        """是否是白天（6:00-18:00）"""
        return 6 <= self.hour < 18
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'HourlyForecast':
        """从字典创建HourlyForecast对象"""
        fx_time_str = data.get('fxTime', '')
        fx_time_str = fx_time_str.replace('+08:00', '+0800')
        
        return cls(
            fxTime=datetime.fromisoformat(fx_time_str) if fx_time_str else datetime.now(),
            temp=float(data.get('temp', 0)),
            icon=data.get('icon', ''),
            text=data.get('text', ''),
            wind360=int(data.get('wind360', 0)),
            windDir=data.get('windDir', ''),
            windScale=data.get('windScale', ''),
            windSpeed=float(data.get('windSpeed', 0)),
            humidity=int(data.get('humidity', 0)),
            precip=float(data.get('precip', 0)),
            pop=int(data.get('pop')) if data.get('pop') else None,
            pressure=int(data.get('pressure', 0)),
            cloud=int(data.get('cloud')) if data.get('cloud') else None,
            dew=float(data.get('dew')) if data.get('dew') else None
        )


@dataclass
class MinutelyPrecip:
    """分钟级降水数据模型"""
    fxTime: datetime
    precip: float
    type: str
    
    # 友好属性名
    @property
    def time(self) -> datetime:
        return self.fxTime
        
    @property
    def forecast_time(self) -> datetime:
        return self.fxTime
        
    @property
    def precipitation(self) -> float:
        return self.precip
        
    @property
    def precipitation_type(self) -> str:
        return self.type
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MinutelyPrecip':
        """从字典创建MinutelyPrecip对象"""
        fx_time_str = data.get('fxTime', '')
        fx_time_str = fx_time_str.replace('+08:00', '+0800')
        
        return cls(
            fxTime=datetime.fromisoformat(fx_time_str) if fx_time_str else datetime.now(),
            precip=float(data.get('precip', 0)),
            type=data.get('type', '')
        )


@dataclass
class Warning:
    """天气预警数据模型"""
    id: str
    sender: str
    pubTime: datetime
    title: str
    startTime: Optional[datetime]
    endTime: Optional[datetime]
    status: str
    level: str  # 已弃用
    severity: str
    severityColor: str
    type: str
    typeName: str
    urgency: str
    certainty: str
    text: str
    related: str
    
    # 友好属性名
    @property
    def warning_id(self) -> str:
        return self.id
        
    @property
    def publish_time(self) -> datetime:
        return self.pubTime
        
    @property
    def start_time(self) -> Optional[datetime]:
        return self.startTime
        
    @property
    def end_time(self) -> Optional[datetime]:
        return self.endTime
        
    @property
    def severity_color(self) -> str:
        return self.severityColor
        
    @property
    def type_id(self) -> str:
        return self.type
        
    @property
    def type_name(self) -> str:
        return self.typeName
        
    @property
    def description(self) -> str:
        return self.text
        
    @property
    def related_id(self) -> str:
        return self.related
    
    # 便捷方法
    @property
    def is_active(self) -> bool:
        """是否有效"""
        return self.status == "active"
    
    @property
    def severity_level(self) -> int:
        """严重程度等级 (1-5)"""
        levels = {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4, "Unknown": 5}
        return levels.get(self.severity, 5)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Warning':
        """从字典创建Warning对象"""
        def parse_time(time_str):
            if time_str:
                return datetime.fromisoformat(time_str.replace('+08:00', '+0800'))
            return None
            
        return cls(
            id=data.get('id', ''),
            sender=data.get('sender', ''),
            pubTime=parse_time(data.get('pubTime', '')),
            title=data.get('title', ''),
            startTime=parse_time(data.get('startTime')),
            endTime=parse_time(data.get('endTime')),
            status=data.get('status', ''),
            level=data.get('level', ''),
            severity=data.get('severity', ''),
            severityColor=data.get('severityColor', ''),
            type=data.get('type', ''),
            typeName=data.get('typeName', ''),
            urgency=data.get('urgency', ''),
            certainty=data.get('certainty', ''),
            text=data.get('text', ''),
            related=data.get('related', '')
        )


@dataclass
class Indices:
    """生活指数数据模型"""
    date: str
    type: str
    name: str
    level: str
    category: str
    text: str
    
    # 友好属性名
    @property
    def index_date(self) -> datetime:
        return datetime.strptime(self.date, '%Y-%m-%d').date()
        
    @property
    def type_id(self) -> str:
        return self.type
        
    @property
    def index_name(self) -> str:
        return self.name
        
    @property
    def index_level(self) -> str:
        return self.level
        
    @property
    def index_category(self) -> str:
        return self.category
        
    @property
    def description(self) -> str:
        return self.text
    
    # 便捷方法
    @property
    def level_int(self) -> int:
        """等级数值"""
        try:
            return int(self.level)
        except:
            return 0
    
    def is_suitable(self) -> bool:
        """是否适宜"""
        return self.category in ["适宜", "较适宜", "良好", "优秀"]
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Indices':
        """从字典创建Indices对象"""
        return cls(
            date=data.get('date', ''),
            type=data.get('type', ''),
            name=data.get('name', ''),
            level=data.get('level', ''),
            category=data.get('category', ''),
            text=data.get('text', '')
        )


@dataclass
class AirQualityIndex:
    """空气质量指数数据模型"""
    code: str
    name: str
    aqi: float
    aqiDisplay: str
    level: str
    category: str
    color: Dict[str, int]
    primaryPollutant: Optional[Dict[str, str]]
    health: Optional[Dict[str, Any]]
    
    # 友好属性名
    @property
    def aqi_code(self) -> str:
        return self.code
        
    @property
    def aqi_name(self) -> str:
        return self.name
        
    @property
    def aqi_value(self) -> float:
        return self.aqi
        
    @property
    def aqi_display(self) -> str:
        return self.aqiDisplay
        
    @property
    def aqi_level(self) -> str:
        return self.level
        
    @property
    def aqi_category(self) -> str:
        return self.category
        
    @property
    def primary_pollutant(self) -> Optional[Dict[str, str]]:
        return self.primaryPollutant
        
    @property
    def health_advice(self) -> Optional[Dict[str, Any]]:
        return self.health
    
    # 便捷方法
    @property
    def is_good(self) -> bool:
        """空气质量是否良好"""
        return self.aqi <= 100
    
    @property
    def health_impact(self) -> str:
        """健康影响"""
        if self.health and 'effect' in self.health:
            return self.health['effect']
        # 默认建议
        if self.aqi <= 50:
            return "空气质量优秀，适合户外活动"
        elif self.aqi <= 100:
            return "空气质量良好，可以正常户外活动"
        elif self.aqi <= 150:
            return "轻度污染，敏感人群减少户外活动"
        elif self.aqi <= 200:
            return "中度污染，避免长时间户外活动"
        elif self.aqi <= 300:
            return "重度污染，减少户外活动"
        else:
            return "严重污染，避免户外活动"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AirQualityIndex':
        """从字典创建AirQualityIndex对象"""
        return cls(
            code=data.get('code', ''),
            name=data.get('name', ''),
            aqi=float(data.get('aqi', 0)),
            aqiDisplay=data.get('aqiDisplay', ''),
            level=data.get('level', ''),
            category=data.get('category', ''),
            color=data.get('color', {}),
            primaryPollutant=data.get('primaryPollutant'),
            health=data.get('health')
        )


@dataclass
class Pollutant:
    """污染物数据模型"""
    code: str
    name: str
    fullName: str
    concentration: Dict[str, Union[float, str]]
    subIndexes: List[Dict[str, Any]]
    
    # 友好属性名
    @property
    def pollutant_code(self) -> str:
        return self.code
        
    @property
    def pollutant_name(self) -> str:
        return self.name
        
    @property
    def full_name(self) -> str:
        return self.fullName
        
    @property
    def concentration_value(self) -> float:
        return self.concentration.get('value', 0)
        
    @property
    def concentration_unit(self) -> str:
        return self.concentration.get('unit', '')
        
    @property
    def sub_indexes(self) -> List[Dict[str, Any]]:
        return self.subIndexes
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Pollutant':
        """从字典创建Pollutant对象"""
        return cls(
            code=data.get('code', ''),
            name=data.get('name', ''),
            fullName=data.get('fullName', ''),
            concentration=data.get('concentration', {}),
            subIndexes=data.get('subIndexes', [])
        )


# ========== 响应包装类==========

@dataclass
class BaseResponse:
    """基础响应类"""
    code: str
    updateTime: str
    fxLink: str
    raw_data: Dict[str, Any] = field(repr=False)
    
    # 友好属性名
    @property
    def status_code(self) -> str:
        return self.code
        
    @property
    def update_time(self) -> str:
        return self.updateTime
        
    @property
    def fx_link(self) -> str:
        return self.fxLink
    
    @property
    def is_success(self) -> bool:
        """检查请求是否成功"""
        return self.code == '200'


@dataclass
class LocationSearchResponse(BaseResponse):
    """城市搜索响应"""
    locations: List[Location]
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LocationSearchResponse':
        """从字典创建LocationSearchResponse对象"""
        locations = [Location.from_dict(loc) for loc in data.get('location', [])]
        return cls(
            code=data.get('code', ''),
            updateTime=data.get('updateTime', ''),
            fxLink=data.get('fxLink', ''),
            locations=locations,
            raw_data=data
        )


@dataclass
class WeatherNowResponse(BaseResponse):
    """实时天气响应"""
    now: WeatherNow
    location_info: Optional[Location] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WeatherNowResponse':
        """从字典创建WeatherNowResponse对象"""
        return cls(
            code=data.get('code', ''),
            updateTime=data.get('updateTime', ''),
            fxLink=data.get('fxLink', ''),
            now=WeatherNow.from_dict(data.get('now', {})),
            raw_data=data
        )


@dataclass
class WeatherDailyResponse(BaseResponse):
    """每日天气预报响应"""
    daily: List[DailyForecast]
    location_info: Optional[Location] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WeatherDailyResponse':
        """从字典创建WeatherDailyResponse对象"""
        daily = [DailyForecast.from_dict(d) for d in data.get('daily', [])]
        return cls(
            code=data.get('code', ''),
            updateTime=data.get('updateTime', ''),
            fxLink=data.get('fxLink', ''),
            daily=daily,
            raw_data=data
        )


@dataclass
class WeatherHourlyResponse(BaseResponse):
    """逐小时天气预报响应"""
    hourly: List[HourlyForecast]
    location_info: Optional[Location] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WeatherHourlyResponse':
        """从字典创建WeatherHourlyResponse对象"""
        hourly = [HourlyForecast.from_dict(h) for h in data.get('hourly', [])]
        return cls(
            code=data.get('code', ''),
            updateTime=data.get('updateTime', ''),
            fxLink=data.get('fxLink', ''),
            hourly=hourly,
            raw_data=data
        )


@dataclass
class MinutelyResponse(BaseResponse):
    """分钟级降水响应"""
    summary: str
    minutely: List[MinutelyPrecip]
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MinutelyResponse':
        """从字典创建MinutelyResponse对象"""
        minutely = [MinutelyPrecip.from_dict(m) for m in data.get('minutely', [])]
        return cls(
            code=data.get('code', ''),
            updateTime=data.get('updateTime', ''),
            fxLink=data.get('fxLink', ''),
            summary=data.get('summary', ''),
            minutely=minutely,
            raw_data=data
        )


@dataclass
class WarningResponse(BaseResponse):
    """天气预警响应"""
    warning: List[Warning]
    location_info: Optional[Location] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WarningResponse':
        """从字典创建WarningResponse对象"""
        warning = [Warning.from_dict(w) for w in data.get('warning', [])]
        return cls(
            code=data.get('code', ''),
            updateTime=data.get('updateTime', ''),
            fxLink=data.get('fxLink', ''),
            warning=warning,
            raw_data=data
        )


@dataclass
class IndicesResponse(BaseResponse):
    """生活指数响应"""
    daily: List[Indices]
    location_info: Optional[Location] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'IndicesResponse':
        """从字典创建IndicesResponse对象"""
        daily = [Indices.from_dict(d) for d in data.get('daily', [])]
        return cls(
            code=data.get('code', ''),
            updateTime=data.get('updateTime', ''),
            fxLink=data.get('fxLink', ''),
            daily=daily,
            raw_data=data
        )


@dataclass
class AirQualityResponse:
    """空气质量响应"""
    indexes: List[AirQualityIndex]
    pollutants: List[Pollutant]
    stations: List[Dict[str, str]]
    metadata: Dict[str, str]
    raw_data: Dict[str, Any] = field(repr=False)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AirQualityResponse':
        """从字典创建AirQualityResponse对象"""
        indexes = [AirQualityIndex.from_dict(idx) for idx in data.get('indexes', [])]
        pollutants = [Pollutant.from_dict(p) for p in data.get('pollutants', [])]
        return cls(
            indexes=indexes,
            pollutants=pollutants,
            stations=data.get('stations', []),
            metadata=data.get('metadata', {}),
            raw_data=data
        )


@dataclass
class AllWeatherData:
    """所有天气数据整合"""
    now: WeatherNowResponse
    daily: WeatherDailyResponse
    hourly: WeatherHourlyResponse
    warning: Optional[WarningResponse] = None
    location_info: Optional[Location] = None


# ========== 异常类 ==========

class QWeatherError(Exception):
    """和风天气API异常基类"""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"QWeather API Error {code}: {message}")


# ========== 缓存系统 ==========

class CacheSystem:
    """缓存系统"""
    
    def __init__(self, ttl: int = 300, max_size: int = 1000, 
                 persist: bool = False, cache_dir: Optional[str] = None):
        self.ttl = ttl
        self.max_size = max_size
        self.persist = persist
        self.cache_dir = Path(cache_dir or tempfile.gettempdir()) / "qweather_cache"
        if persist:
            self.cache_dir.mkdir(exist_ok=True)
        self._memory_cache = {}
        self._access_times = {}
        
    def _get_file_path(self, key: str) -> Path:
        """获取缓存文件路径"""
        key_hash = hashlib.md5(key.encode()).hexdigest()
        return self.cache_dir / f"{key_hash}.cache"
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        # 先检查内存缓存
        if key in self._memory_cache:
            data, timestamp = self._memory_cache[key]
            if time.time() - timestamp < self.ttl:
                self._access_times[key] = time.time()
                return data
            else:
                del self._memory_cache[key]
        
        # 检查持久化缓存
        if self.persist:
            file_path = self._get_file_path(key)
            if file_path.exists():
                try:
                    with open(file_path, 'rb') as f:
                        data, timestamp = pickle.load(f)
                    if time.time() - timestamp < self.ttl:
                        # 加载到内存缓存
                        self._memory_cache[key] = (data, timestamp)
                        self._access_times[key] = time.time()
                        self._check_size()
                        return data
                    else:
                        file_path.unlink()
                except:
                    pass
        
        return None
    
    def set(self, key: str, value: Any):
        """设置缓存"""
        timestamp = time.time()
        self._memory_cache[key] = (value, timestamp)
        self._access_times[key] = timestamp
        
        # 持久化
        if self.persist:
            file_path = self._get_file_path(key)
            try:
                with open(file_path, 'wb') as f:
                    pickle.dump((value, timestamp), f)
            except:
                pass
        
        self._check_size()
    
    def _check_size(self):
        """检查缓存大小，LRU淘汰"""
        if len(self._memory_cache) > self.max_size:
            # 按访问时间排序
            sorted_keys = sorted(self._access_times.items(), key=lambda x: x[1])
            # 删除最久未访问的
            for key, _ in sorted_keys[:len(self._memory_cache) - self.max_size]:
                del self._memory_cache[key]
                del self._access_times[key]
                if self.persist:
                    file_path = self._get_file_path(key)
                    if file_path.exists():
                        file_path.unlink()
    
    def clear(self):
        """清空缓存"""
        self._memory_cache.clear()
        self._access_times.clear()
        if self.persist:
            for file in self.cache_dir.glob("*.cache"):
                file.unlink()


# ========== 主客户端类 ==========

class QWeather:
    """
    和风天气API客户端
    
    Args:
        api_key: API密钥（支持从环境变量QWEATHER_KEY读取）
        api_host: API主机地址（支持从环境变量QWEATHER_HOST读取）
        jwt_token: JWT令牌（使用JWT认证时必填）
        use_jwt: 是否使用JWT认证，默认False
        timeout: 请求超时时间（秒），默认30秒
        return_raw: 是否返回原始JSON数据，默认False
        cache_enabled: 是否启用缓存，默认True
        cache_ttl: 缓存过期时间（秒），默认300秒
        cache_persist: 是否持久化缓存，默认False
        retry_count: 重试次数，默认3次
        retry_delay: 重试延迟（秒），默认1秒
    
    Examples:
        # 使用API Key认证
        >>> client = QWeather(api_key="your_key", api_host="your_host")
        
        # 使用JWT认证
        >>> client = QWeather(jwt_token="your_jwt", api_host="your_host", use_jwt=True)
        
        # 从环境变量读取
        >>> os.environ['QWEATHER_KEY'] = 'your_key'
        >>> os.environ['QWEATHER_HOST'] = 'your_host'
        >>> client = QWeather()
    """
    
    # 错误消息映射
    ERROR_MESSAGES = {
        '204': '请求成功但该地区暂无数据',
        '400': '请求错误 - 参数无效',
        '401': '认证失败 - 请检查API Key或JWT Token',
        '402': '额度不足或超过访问次数限制',
        '403': '访问被拒绝 - 请检查IP白名单或域名绑定',
        '404': '数据或地区不存在',
        '429': '请求过于频繁 - 超过QPM限制',
        '500': '服务器内部错误'
    }
    
    def __init__(
        self, 
        api_key: str = None, 
        api_host: str = None, 
        jwt_token: str = None, 
        use_jwt: bool = False, 
        timeout: int = 30, 
        return_raw: bool = False,
        cache_enabled: bool = True,
        cache_ttl: int = 300,
        cache_persist: bool = False,
        retry_count: int = 3,
        retry_delay: int = 1
    ):
        """初始化和风天气客户端"""
        # 支持从环境变量读取配置
        if not api_host:
            api_host = os.getenv('QWEATHER_HOST')
        if not api_host:
            raise ValueError("api_host is required")
        
        # 确保api_host有正确的scheme
        if not api_host.startswith(('http://', 'https://')):
            api_host = 'https://' + api_host
            
        self.api_host = api_host.rstrip('/')
        self.timeout = timeout
        self.use_jwt = use_jwt
        self.return_raw = return_raw
        self.cache_enabled = cache_enabled
        self.cache_ttl = cache_ttl
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        
        # 认证设置
        if use_jwt:
            if not jwt_token:
                jwt_token = os.getenv('QWEATHER_JWT')
            if not jwt_token:
                raise ValueError("jwt_token is required when use_jwt=True")
            self.jwt_token = jwt_token
            self.api_key = None
        else:
            if not api_key:
                api_key = os.getenv('QWEATHER_KEY')
            if not api_key:
                raise ValueError("api_key is required when use_jwt=False")
            self.api_key = api_key
            self.jwt_token = None
            
        # 初始化session
        self.session = requests.Session()
        self._setup_headers()
        
        # 缓存系统
        if cache_enabled:
            self.cache = CacheSystem(
                ttl=cache_ttl,
                persist=cache_persist
            )
        else:
            self.cache = None
            
        # 当前位置（用于链式调用）
        self._current_location = None
        
    def _setup_headers(self):
        """设置请求头"""
        self.session.headers.update({
            'Accept-Encoding': 'gzip',
            'User-Agent': 'QWeather-Python-SDK/4.0.0'
        })
        
        if self.use_jwt:
            self.session.headers['Authorization'] = f'Bearer {self.jwt_token}'
        else:
            self.session.headers['X-QW-Api-Key'] = self.api_key
    
    # ========== 链式调用支持 ==========
    
    def location(self, location: Union[str, Location]) -> 'QWeather':
        """
        设置位置（支持链式调用）
        
        Args:
            location: 位置（城市名、坐标、Location对象）
            
        Returns:
            self: 返回自身以支持链式调用
            
        Example:
            >>> client.location("北京").get_weather_now()
        """
        self._current_location = location
        return self
    
    # ========== API方法==========
    
    def search_city(
        self, 
        location: str, 
        adm: Optional[str] = None, 
        range: Optional[str] = None, 
        number: int = 10,
        lang: Optional[Lang] = None
    ) -> Union[LocationSearchResponse, Dict[str, Any]]:
        """
        城市搜索（原方法名）
        
        搜索全球城市，支持模糊搜索、多语言、经纬度反查等功能
        
        Args:
            location: 需要查询的城市名称、LocationID、IP地址、经纬度坐标等
            adm: 城市的上级行政区划，用于过滤重名城市
            range: 搜索范围，ISO 3166 国家代码
            number: 返回结果数量，1-20，默认10
            lang: 多语言设置
            
        Returns:
            LocationSearchResponse对象或原始JSON
        """
        params = {
            'location': location,
            'number': number
        }
        
        if adm:
            params['adm'] = adm
        if range:
            params['range'] = range
        if lang:
            params['lang'] = lang.value
            
        data = self._make_request('/geo/v2/city/lookup', params)
        return data if self.return_raw else LocationSearchResponse.from_dict(data)
    
    # 增加别名方法，使用更友好
    def search(self, keyword: str, **kwargs) -> Union[LocationSearchResponse, Dict[str, Any]]:
        """search_city的别名"""
        return self.search_city(keyword, **kwargs)
    
    def get_weather_now(
        self, 
        location: str = None, 
        lang: Optional[Lang] = None,
        unit: Unit = Unit.METRIC
    ) -> Union[WeatherNowResponse, Dict[str, Any]]:
        """
        获取实时天气（原方法名）
        
        Args:
            location: 城市名称、LocationID或经纬度坐标
            lang: 多语言设置
            unit: 单位设置
            
        Returns:
            WeatherNowResponse对象或原始JSON
        """
        location = location or self._current_location
        if not location:
            raise ValueError("Location is required")
            
        # 解析location
        resolved_location, location_info = self._resolve_location(location, lang)
        
        params = {
            'location': resolved_location,
            'unit': unit.value
        }
        
        if lang:
            params['lang'] = lang.value
            
        data = self._make_request('/v7/weather/now', params)
        
        if self.return_raw:
            if location_info:
                data['_location_info'] = location_info.__dict__
            return data
        else:
            response = WeatherNowResponse.from_dict(data)
            response.location_info = location_info
            return response
    
    # 增加别名方法
    def now(self, location: str = None, **kwargs) -> Union[WeatherNowResponse, Dict[str, Any]]:
        """get_weather_now的别名"""
        return self.get_weather_now(location, **kwargs)
    
    def get_weather_daily(
        self, 
        location: str = None, 
        days: str = '3d',
        lang: Optional[Lang] = None, 
        unit: Unit = Unit.METRIC
    ) -> Union[WeatherDailyResponse, Dict[str, Any]]:
        """
        获取每日天气预报
        
        Args:
            location: 城市名称、LocationID或经纬度坐标
            days: 预报天数，可选值：3d, 7d, 10d, 15d, 30d
            lang: 多语言设置
            unit: 单位设置
            
        Returns:
            WeatherDailyResponse对象或原始JSON
        """
        location = location or self._current_location
        if not location:
            raise ValueError("Location is required")
            
        if days not in ['3d', '7d', '10d', '15d', '30d']:
            raise ValueError("days must be one of: 3d, 7d, 10d, 15d, 30d")
        
        # 解析location
        resolved_location, location_info = self._resolve_location(location, lang)
            
        params = {
            'location': resolved_location,
            'unit': unit.value
        }
        
        if lang:
            params['lang'] = lang.value
            
        data = self._make_request(f'/v7/weather/{days}', params)
        
        if self.return_raw:
            if location_info:
                data['_location_info'] = location_info.__dict__
            return data
        else:
            response = WeatherDailyResponse.from_dict(data)
            response.location_info = location_info
            return response
    
    # 增加别名方法
    def daily(self, location: str = None, days: int = 7, **kwargs) -> Union[WeatherDailyResponse, Dict[str, Any]]:
        """get_weather_daily的别名，days参数改为数字"""
        days_map = {3: '3d', 7: '7d', 10: '10d', 15: '15d', 30: '30d'}
        days_str = days_map.get(days, '7d')
        return self.get_weather_daily(location, days_str, **kwargs)
    
    def get_weather_hourly(
        self, 
        location: str = None, 
        hours: str = '24h',
        lang: Optional[Lang] = None, 
        unit: Unit = Unit.METRIC
    ) -> Union[WeatherHourlyResponse, Dict[str, Any]]:
        """
        获取逐小时天气预报
        
        Args:
            location: 城市名称、LocationID或经纬度坐标
            hours: 预报小时数，可选值：24h, 72h, 168h
            lang: 多语言设置
            unit: 单位设置
            
        Returns:
            WeatherHourlyResponse对象或原始JSON
        """
        location = location or self._current_location
        if not location:
            raise ValueError("Location is required")
            
        if hours not in ['24h', '72h', '168h']:
            raise ValueError("hours must be one of: 24h, 72h, 168h")
        
        # 解析location
        resolved_location, location_info = self._resolve_location(location, lang)
            
        params = {
            'location': resolved_location,
            'unit': unit.value
        }
        
        if lang:
            params['lang'] = lang.value
            
        data = self._make_request(f'/v7/weather/{hours}', params)
        
        if self.return_raw:
            if location_info:
                data['_location_info'] = location_info.__dict__
            return data
        else:
            response = WeatherHourlyResponse.from_dict(data)
            response.location_info = location_info
            return response
    
    # 增加别名方法
    def hourly(self, location: str = None, hours: int = 24, **kwargs) -> Union[WeatherHourlyResponse, Dict[str, Any]]:
        """get_weather_hourly的别名，hours参数改为数字"""
        hours_map = {24: '24h', 72: '72h', 168: '168h'}
        hours_str = hours_map.get(hours, '24h')
        return self.get_weather_hourly(location, hours_str, **kwargs)
    
    def get_weather_minutely(
        self, 
        location: str = None, 
        lang: Optional[Lang] = None
    ) -> Union[MinutelyResponse, Dict[str, Any]]:
        """
        获取分钟级降水预报
        
        提供中国地区未来2小时内每5分钟的降水预报
        
        Args:
            location: 经纬度坐标或城市名称
            lang: 多语言设置
            
        Returns:
            MinutelyResponse对象或原始JSON
        """
        location = location or self._current_location
        if not location:
            raise ValueError("Location is required")
            
        # 如果不是坐标，需要先搜索获取坐标
        if not self._is_coordinate(location):
            _, location_info = self._resolve_location(location, lang)
            if location_info:
                location = f"{location_info.lat},{location_info.lon}"
            else:
                raise QWeatherError('400', '无法获取坐标信息')
        
        params = {
            'location': location
        }
        
        if lang:
            params['lang'] = lang.value
            
        data = self._make_request('/v7/minutely/5m', params)
        return data if self.return_raw else MinutelyResponse.from_dict(data)
    
    # 增加别名方法
    def minutely(self, location: str = None, **kwargs) -> Union[MinutelyResponse, Dict[str, Any]]:
        """get_weather_minutely的别名"""
        return self.get_weather_minutely(location, **kwargs)
    
    def get_weather_warning(
        self, 
        location: str = None, 
        lang: Optional[Lang] = None
    ) -> Union[WarningResponse, Dict[str, Any]]:
        """
        获取天气灾害预警
        
        Args:
            location: 城市名称、LocationID或经纬度坐标
            lang: 多语言设置
            
        Returns:
            WarningResponse对象或原始JSON
        """
        location = location or self._current_location
        if not location:
            raise ValueError("Location is required")
            
        # 解析location
        resolved_location, location_info = self._resolve_location(location, lang)
        
        params = {
            'location': resolved_location
        }
        
        if lang:
            params['lang'] = lang.value
            
        data = self._make_request('/v7/warning/now', params)
        
        if self.return_raw:
            if location_info:
                data['_location_info'] = location_info.__dict__
            return data
        else:
            response = WarningResponse.from_dict(data)
            response.location_info = location_info
            return response
    
    # 增加别名方法
    def warnings(self, location: str = None, **kwargs) -> Union[WarningResponse, Dict[str, Any]]:
        """get_weather_warning的别名"""
        return self.get_weather_warning(location, **kwargs)
    
    def get_indices(
        self, 
        location: str = None, 
        type: Union[str, List[str], IndicesType, List[IndicesType]] = IndicesType.ALL, 
        days: str = '1d', 
        lang: Optional[Lang] = None
    ) -> Union[IndicesResponse, Dict[str, Any]]:
        """
        获取天气生活指数
        
        Args:
            location: 城市名称、LocationID或经纬度坐标
            type: 指数类型
            days: 预报天数，可选值：1d, 3d
            lang: 多语言设置
            
        Returns:
            IndicesResponse对象或原始JSON
        """
        location = location or self._current_location
        if not location:
            raise ValueError("Location is required")
            
        if days not in ['1d', '3d']:
            raise ValueError("days must be one of: 1d, 3d")
        
        # 解析location
        resolved_location, location_info = self._resolve_location(location, lang)
            
        # 处理type参数
        if isinstance(type, (list, tuple)):
            type_values = []
            for t in type:
                if isinstance(t, IndicesType):
                    type_values.append(t.value)
                else:
                    type_values.append(str(t))
            type_str = ','.join(type_values)
        elif isinstance(type, IndicesType):
            type_str = type.value
        else:
            type_str = str(type)
            
        params = {
            'location': resolved_location,
            'type': type_str
        }
        
        if lang:
            params['lang'] = lang.value
            
        data = self._make_request(f'/v7/indices/{days}', params)
        
        if self.return_raw:
            if location_info:
                data['_location_info'] = location_info.__dict__
            return data
        else:
            response = IndicesResponse.from_dict(data)
            response.location_info = location_info
            return response
    
    # 增加别名方法
    def indices(self, location: str = None, types = None, days: int = 1, **kwargs) -> Union[IndicesResponse, Dict[str, Any]]:
        """get_indices的别名"""
        days_str = '1d' if days == 1 else '3d'
        type_param = types if types is not None else IndicesType.ALL
        return self.get_indices(location, type_param, days_str, **kwargs)
    
    def get_air_quality_now(
        self, 
        location: str = None, 
        lang: Optional[Lang] = None
    ) -> Union[AirQualityResponse, Dict[str, Any]]:
        """
        获取实时空气质量
        
        Args:
            location: 经纬度坐标或城市名称
            lang: 多语言设置
            
        Returns:
            AirQualityResponse对象或原始JSON
        """
        location = location or self._current_location
        if not location:
            raise ValueError("Location is required")
            
        # 如果不是坐标，需要先搜索获取坐标
        if not self._is_coordinate(location):
            _, location_info = self._resolve_location(location, lang)
            if location_info:
                location = f"{location_info.lat},{location_info.lon}"
            else:
                raise QWeatherError('400', '无法获取坐标信息')
        
        # 解析经纬度
        try:
            lat, lon = location.split(',')
            lat = lat.strip()
            lon = lon.strip()
        except ValueError:
            raise ValueError("location必须是'纬度,经度'格式")
            
        endpoint = f'/airquality/v1/current/{lat}/{lon}'
        params = {}
        
        if lang:
            params['lang'] = lang.value
            
        data = self._make_request(endpoint, params if params else None)
        return data if self.return_raw else AirQualityResponse.from_dict(data)
    
    # 增加别名方法
    def air_quality(self, location: str = None, **kwargs) -> Union[AirQualityResponse, Dict[str, Any]]:
        """get_air_quality_now的别名"""
        return self.get_air_quality_now(location, **kwargs)
    
    # ========== 便捷方法==========
    
    def get_weather_3days(self, location: str = None, **kwargs) -> Union[WeatherDailyResponse, Dict[str, Any]]:
        """获取3天天气预报"""
        return self.get_weather_daily(location, '3d', **kwargs)
    
    def get_weather_7days(self, location: str = None, **kwargs) -> Union[WeatherDailyResponse, Dict[str, Any]]:
        """获取7天天气预报"""
        return self.get_weather_daily(location, '7d', **kwargs)
    
    def get_weather_10days(self, location: str = None, **kwargs) -> Union[WeatherDailyResponse, Dict[str, Any]]:
        """获取10天天气预报"""
        return self.get_weather_daily(location, '10d', **kwargs)
    
    def get_weather_15days(self, location: str = None, **kwargs) -> Union[WeatherDailyResponse, Dict[str, Any]]:
        """获取15天天气预报"""
        return self.get_weather_daily(location, '15d', **kwargs)
    
    def get_weather_30days(self, location: str = None, **kwargs) -> Union[WeatherDailyResponse, Dict[str, Any]]:
        """获取30天天气预报"""
        return self.get_weather_daily(location, '30d', **kwargs)
    
    def get_weather_24hours(self, location: str = None, **kwargs) -> Union[WeatherHourlyResponse, Dict[str, Any]]:
        """获取24小时天气预报"""
        return self.get_weather_hourly(location, '24h', **kwargs)
    
    def get_weather_72hours(self, location: str = None, **kwargs) -> Union[WeatherHourlyResponse, Dict[str, Any]]:
        """获取72小时天气预报"""
        return self.get_weather_hourly(location, '72h', **kwargs)
    
    def get_weather_168hours(self, location: str = None, **kwargs) -> Union[WeatherHourlyResponse, Dict[str, Any]]:
        """获取168小时天气预报"""
        return self.get_weather_hourly(location, '168h', **kwargs)
    
    def search_city_by_name(self, city_name: str, country: Optional[str] = None, **kwargs) -> Union[LocationSearchResponse, Dict[str, Any]]:
        """根据城市名称搜索"""
        return self.search_city(city_name, range=country, **kwargs)
    
    def get_all_weather_data(
        self, 
        location: str = None, 
        lang: Optional[Lang] = None,
        unit: Unit = Unit.METRIC
    ) -> Union[AllWeatherData, Dict[str, Any]]:
        """
        获取所有天气数据
        
        一次性获取实时天气、7天预报、24小时预报和天气预警
        """
        location = location or self._current_location
        if not location:
            raise ValueError("Location is required")
            
        # 先解析location，避免重复搜索
        resolved_location, location_info = self._resolve_location(location, lang)
        
        if self.return_raw:
            result = {
                'now': self.get_weather_now(resolved_location, lang, unit),
                'daily': self.get_weather_7days(resolved_location, lang=lang, unit=unit),
                'hourly': self.get_weather_24hours(resolved_location, lang=lang, unit=unit)
            }
            
            # 尝试获取预警信息
            try:
                result['warning'] = self.get_weather_warning(resolved_location, lang)
            except QWeatherError:
                result['warning'] = None
                
            if location_info:
                result['_location_info'] = location_info.__dict__
                
            return result
        else:
            now = self.get_weather_now(resolved_location, lang, unit)
            daily = self.get_weather_7days(resolved_location, lang=lang, unit=unit)
            hourly = self.get_weather_24hours(resolved_location, lang=lang, unit=unit)
            
            # 尝试获取预警信息
            try:
                warning = self.get_weather_warning(resolved_location, lang)
            except QWeatherError:
                warning = None
                
            return AllWeatherData(
                now=now,
                daily=daily,
                hourly=hourly,
                warning=warning,
                location_info=location_info
            )
    
    # 新的便捷方法
    def today(self, location: str = None, **kwargs) -> DailyForecast:
        """获取今日天气"""
        response = self.get_weather_daily(location, '3d', **kwargs)
        if self.return_raw:
            return response['daily'][0] if response.get('daily') else None
        return response.daily[0] if response.daily else None
    
    def tomorrow(self, location: str = None, **kwargs) -> DailyForecast:
        """获取明日天气"""
        response = self.get_weather_daily(location, '3d', **kwargs)
        if self.return_raw:
            return response['daily'][1] if len(response.get('daily', [])) > 1 else None
        return response.daily[1] if len(response.daily) > 1 else None
    
    # ========== 批量查询功能 ==========
    
    def batch_now(self, locations: List[Union[str, Location]], 
                  max_workers: int = 5, **kwargs) -> Dict[str, Union[WeatherNowResponse, Dict[str, Any]]]:
        """
        批量获取实时天气
        
        Args:
            locations: 位置列表
            max_workers: 最大并发数
            **kwargs: 传递给get_weather_now的其他参数
            
        Returns:
            Dict[str, WeatherNowResponse]: 位置->天气映射
        """
        results = {}
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_location = {
                executor.submit(self.get_weather_now, loc, **kwargs): str(loc) 
                for loc in locations
            }
            
            for future in as_completed(future_to_location):
                location = future_to_location[future]
                try:
                    weather = future.result()
                    results[location] = weather
                except Exception as e:
                    logger.error(f"Failed to get weather for {location}: {e}")
                    results[location] = None
                    
        return results
    
    def batch_daily(self, locations: List[Union[str, Location]], 
                    days: str = '7d', max_workers: int = 5, **kwargs) -> Dict[str, Union[WeatherDailyResponse, Dict[str, Any]]]:
        """批量获取每日预报"""
        results = {}
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_location = {
                executor.submit(self.get_weather_daily, loc, days, **kwargs): str(loc) 
                for loc in locations
            }
            
            for future in as_completed(future_to_location):
                location = future_to_location[future]
                try:
                    weather = future.result()
                    results[location] = weather
                except Exception as e:
                    logger.error(f"Failed to get daily forecast for {location}: {e}")
                    results[location] = None
                    
        return results
    
    # ========== 内部方法 ==========
    
    def _get_cache_key(self, endpoint: str, params: Dict[str, Any]) -> str:
        """生成缓存键"""
        param_str = urlencode(sorted(params.items())) if params else ""
        return f"{endpoint}?{param_str}"
    
    def _make_request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        发送HTTP请求（带缓存和重试）
        
        Args:
            endpoint: API端点路径
            params: 请求参数
            
        Returns:
            解析后的JSON响应
        """
        # 检查缓存
        if self.cache_enabled and self.cache:
            cache_key = self._get_cache_key(endpoint, params)
            cached_data = self.cache.get(cache_key)
            if cached_data:
                logger.debug(f"Cache hit for {cache_key}")
                return cached_data
        
        url = f"{self.api_host}{endpoint}"
        last_error = None
        
        for retry in range(self.retry_count):
            try:
                logger.debug(f"Making request to {url} with params {params} (attempt {retry + 1})")
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                
                # 智能处理响应内容
                content = response.content
                
                # 检查是否是gzip压缩的内容
                if response.headers.get('Content-Encoding') == 'gzip' or content[:2] == b'\x1f\x8b':
                    try:
                        content = gzip.decompress(content)
                    except Exception:
                        pass
                
                # 解析JSON
                try:
                    result = json.loads(content.decode('utf-8'))
                except UnicodeDecodeError:
                    result = json.loads(content.decode('gbk'))
                    
                # 检查API返回的状态码
                if 'code' in result and result['code'] != '200':
                    self._handle_error_code(result['code'])
                
                # 保存到缓存
                if self.cache_enabled and self.cache:
                    self.cache.set(cache_key, result)
                    
                return result
                
            except requests.exceptions.Timeout:
                last_error = QWeatherError('TIMEOUT', f"请求超时（{self.timeout}秒）")
            except requests.exceptions.ConnectionError:
                last_error = QWeatherError('CONNECTION_ERROR', "网络连接错误")
            except requests.exceptions.RequestException as e:
                last_error = QWeatherError('NETWORK_ERROR', f"网络错误: {str(e)}")
            except QWeatherError as e:
                last_error = e
                
            # 如果不是最后一次重试，等待后重试
            if retry < self.retry_count - 1:
                wait_time = self.retry_delay * (2 ** retry)  # 指数退避
                logger.warning(f"Request failed, retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                
        # 所有重试都失败
        raise last_error
    
    def _handle_error_code(self, code: str):
        """处理API错误码"""
        message = self.ERROR_MESSAGES.get(code, f'未知错误代码: {code}')
        raise QWeatherError(code, message)
    
    def _is_location_id(self, location: str) -> bool:
        """判断location是否为LocationID"""
        return location.isdigit() and len(location) >= 6
    
    def _is_coordinate(self, location: str) -> bool:
        """判断location是否为坐标"""
        coord_pattern = r'^-?\d+\.?\d*,-?\d+\.?\d*$'
        return bool(re.match(coord_pattern, location))
    
    @lru_cache(maxsize=100)
    def _resolve_location(self, location: Union[str, Location], lang: Optional[Lang] = None) -> Tuple[str, Optional[Location]]:
        """
        解析location参数
        
        如果是城市名称则搜索获取LocationID，如果是LocationID或坐标则直接返回
        """
        # 如果是Location对象
        if isinstance(location, Location):
            return location.id or location.coordinates_string, location
            
        # 如果是LocationID或坐标，直接返回
        if self._is_location_id(location) or self._is_coordinate(location):
            return location, None
            
        # 否则视为城市名称，进行搜索
        logger.debug(f"Searching city: {location}")
        search_result = self.search_city(location, lang=lang)
        
        if isinstance(search_result, dict):
            # 返回原始数据的情况
            if search_result.get('code') == '200' and search_result.get('location'):
                location_data = search_result['location'][0]
                location_info = Location.from_dict(location_data)
                return location_data['id'], location_info
            else:
                raise QWeatherError('404', f"城市 '{location}' 未找到")
        else:
            # 返回对象的情况
            if search_result.is_success and search_result.locations:
                location_info = search_result.locations[0]
                return location_info.id, location_info
            else:
                raise QWeatherError('404', f"城市 '{location}' 未找到")
    
    def clear_cache(self):
        """清空缓存"""
        if self.cache:
            self.cache.clear()
            logger.info("Cache cleared")


# ========== 快捷函数 ==========

def quick_weather(location: str, api_key: str = None, api_host: str = None, 
                 lang: Lang = Lang.ZH, unit: Unit = Unit.METRIC) -> WeatherNow:
    """
    快速获取天气（无需创建客户端）
    
    Args:
        location: 位置
        api_key: API密钥（可选，从环境变量读取）
        api_host: API主机（可选，从环境变量读取）
        lang: 语言设置
        unit: 单位设置
        
    Returns:
        WeatherNow: 实时天气对象
        
    Example:
        >>> weather = quick_weather("北京")
        >>> print(f"{weather.temperature}°C {weather.text}")
    """
    client = QWeather(api_key=api_key, api_host=api_host)
    response = client.get_weather_now(location, lang=lang, unit=unit)
    return response.now if hasattr(response, 'now') else WeatherNow.from_dict(response['now'])


def weather_report(location: str, api_key: str = None, api_host: str = None,
                  lang: Lang = Lang.ZH, unit: Unit = Unit.METRIC) -> str:
    """
    生成天气报告
    
    Args:
        location: 位置
        api_key: API密钥
        api_host: API主机
        lang: 语言设置
        unit: 单位设置
        
    Returns:
        str: 格式化的天气报告
    """
    client = QWeather(api_key=api_key, api_host=api_host)
    
    # 获取数据
    now_response = client.get_weather_now(location, lang=lang, unit=unit)
    daily_response = client.get_weather_daily(location, '3d', lang=lang, unit=unit)
    warnings_response = client.get_weather_warning(location, lang=lang)
    
    now = now_response.now if hasattr(now_response, 'now') else WeatherNow.from_dict(now_response['now'])
    daily = daily_response.daily if hasattr(daily_response, 'daily') else [DailyForecast.from_dict(d) for d in daily_response['daily']]
    warnings = warnings_response.warning if hasattr(warnings_response, 'warning') else []
    
    # 生成报告
    report = []
    report.append(f"=== {location} 天气报告 ===")
    report.append(f"\n【实时天气】")
    report.append(f"温度: {now.temperature}°C (体感 {now.feels_like}°C)")
    report.append(f"天气: {now.text}")
    report.append(f"风向风速: {now.wind_direction} {now.wind_speed}km/h")
    report.append(f"湿度: {now.humidity}%")
    report.append(f"气压: {now.pressure}hPa")
    report.append(f"能见度: {now.visibility}km")
    
    if daily:
        today = daily[0]
        report.append(f"\n【今日天气】")
        report.append(f"温度范围: {today.temperature_range}")
        report.append(f"白天: {today.day_weather}")
        report.append(f"夜间: {today.night_weather}")
        report.append(f"日出日落: {today.sunrise} - {today.sunset}")
        
        if len(daily) > 1:
            tomorrow = daily[1]
            report.append(f"\n【明日天气】")
            report.append(f"温度范围: {tomorrow.temperature_range}")
            report.append(f"白天: {tomorrow.day_weather}")
            report.append(f"夜间: {tomorrow.night_weather}")
    
    if warnings:
        report.append(f"\n【天气预警】")
        for w in warnings:
            report.append(f"⚠️ {w.title}")
            report.append(f"   发布时间: {w.publish_time}")
            report.append(f"   严重程度: {w.severity} ({w.severity_color})")
            
    return '\n'.join(report)


# ========== 使用示例 ==========

def main():
    """使用示例"""
    
    # 设置API凭据（实际使用时请设置环境变量 QWEATHER_KEY 和 QWEATHER_HOST）
    
    # 初始化客户端
    client = QWeather()
    
    try:
        # ===== 使用原方法名 =====
        print("="*50)
        print("使用原方法名")
        print("="*50)
        
        # 实时天气
        weather = client.get_weather_now("北京")
        if weather.is_success:
            print(f"温度: {weather.now.temp}°C")
            print(f"体感温度: {weather.now.feelsLike}°C")
            print(f"天气: {weather.now.text}")
        
        # ===== 使用友好的属性名 =====
        print("\n" + "="*50)
        print("使用友好的属性名")
        print("="*50)
        
        # 同样的数据，使用友好属性名访问
        print(f"温度: {weather.now.temperature}°C")
        print(f"体感温度: {weather.now.feels_like}°C")
        print(f"天气: {weather.now.weather_text}")
        print(f"是否在下雨: {weather.now.is_raining}")
        print(f"是否好天气: {weather.now.is_good_weather}")
        
        # ===== 使用简化的方法名 =====
        print("\n" + "="*50)
        print("使用简化的方法名")
        print("="*50)
        
        # 使用别名方法
        current = client.now("上海")
        daily = client.daily("上海", days=7)
        hourly = client.hourly("上海", hours=24)
        
        print(f"上海现在: {current.now.temperature}°C {current.now.text}")
        print(f"未来7天: {len(daily.daily)}天预报")
        print(f"未来24小时: {len(hourly.hourly)}小时预报")
        
        # ===== 链式调用 =====
        print("\n" + "="*50)
        print("链式调用")
        print("="*50)
        
        # 设置位置后连续查询
        client.location("广州")
        now = client.now()
        today = client.today()
        tomorrow = client.tomorrow()
        
        print(f"广州现在: {now.now.temperature}°C")
        print(f"今日: {today.temperature_range} {today.day_weather}")
        print(f"明日: {tomorrow.temperature_range} {tomorrow.day_weather}")
        
        # ===== 批量查询 =====
        print("\n" + "="*50)
        print("批量查询")
        print("="*50)
        
        cities = ["北京", "上海", "广州", "深圳"]
        results = client.batch_now(cities)
        
        for city, weather in results.items():
            if weather and weather.is_success:
                print(f"{city}: {weather.now.temperature}°C {weather.now.text}")
        
        # ===== 快捷函数 =====
        print("\n" + "="*50)
        print("快捷函数")
        print("="*50)
        
        # 一行获取天气
        weather = quick_weather("北京")
        print(f"北京: {weather.temperature}°C {weather.text}")
        
        # 生成天气报告
        report = weather_report("北京")
        print(report)
        
        # ===== 高级功能 =====
        print("\n" + "="*50)
        print("高级功能")
        print("="*50)
        
        # 搜索城市
        locations = client.search("beijing")
        for loc in locations.locations[:3]:
            print(f"{loc.name} - {loc.country} ({loc.latitude}, {loc.longitude})")
        
        # 生活指数
        indices = client.indices("北京", types=[IndicesType.UV, IndicesType.DRESSING])
        for index in indices.daily:
            print(f"{index.name}: {index.category} - {index.text[:30]}...")
        
        # 空气质量
        try:
            aqi = client.air_quality("北京")
            if aqi.indexes:
                index = aqi.indexes[0]
                print(f"AQI: {index.aqi} ({index.category})")
                print(f"健康影响: {index.health_impact}")
        except:
            pass
            
    except QWeatherError as e:
        print(f"API错误: {e}")
    except Exception as e:
        print(f"发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()