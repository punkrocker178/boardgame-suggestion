SAMPLE_THING_XML = """<?xml version="1.0" encoding="utf-8"?>
<items termsofuse="https://boardgamegeek.com/xmlapi/termsofuse">
  <item type="boardgame" id="224517">
    <thumbnail>https://example.com/thumb.jpg</thumbnail>
    <image>https://example.com/image.jpg</image>
    <name type="primary" sortindex="1" value="Brass: Birmingham"/>
    <description>Build networks during the industrial revolution.</description>
    <yearpublished value="2018"/>
    <minplayers value="2"/>
    <maxplayers value="4"/>
    <playingtime value="120"/>
    <minplaytime value="60"/>
    <maxplaytime value="120"/>
    <minage value="14"/>
    <link type="boardgamecategory" id="1021" value="Economic"/>
    <link type="boardgamecategory" id="1086" value="Territory Building"/>
    <link type="boardgamemechanic" id="2081" value="Route/Network Building"/>
    <statistics>
      <ratings>
        <averageweight value="3.86"/>
      </ratings>
    </statistics>
  </item>
</items>
"""

EMPTY_PLAYTIME_XML = """<?xml version="1.0" encoding="utf-8"?>
<items termsofuse="https://boardgamegeek.com/xmlapi/termsofuse">
  <item type="boardgame" id="999">
    <name type="primary" sortindex="1" value="Test Game"/>
    <minplayers value="2"/>
    <maxplayers value="4"/>
    <playingtime value="0"/>
    <minplaytime value="45"/>
    <maxplaytime value="90"/>
  </item>
</items>
"""
