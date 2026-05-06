# config模版
BASE_URL = "https://bbs.nga.cn"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"

# nga网站个人cookie项目
NGAPASSPORTUID = ""
NGAPASSPORTCID = ""


# 保存目录
OUTPUT_DIR = "output"


#html模板

PDF_PAGE_SIZE = "A4"
PDF_PAGE_MARGIN = "12mm"
PDF_LONG_IMAGE_MIN_WIDTH = 800
PDF_LONG_IMAGE_MIN_RATIO = 4.0
PDF_LONG_IMAGE_SLICE_RATIO = 1.35
PDF_SPEAKER_PORTRAIT_MAX_DIMENSION = 640
PDF_SPEAKER_PORTRAIT_MAX_RATIO = 3.0
PDF_SPEAKER_PORTRAIT_SIZE = "14mm"

HTML_PRE = '<div class="bbcode_container">'
HTML_POST = "</div>"
HTML_STYLE = """
<style>
@page {
    size: __PDF_PAGE_SIZE__;
    margin: __PDF_PAGE_MARGIN__;
}

.bbcode_container, html, body {
  background-color: #ffffff;
}

body{
  color: #111111;
  font-size: 10.5pt;
  line-height: 1.55;
  margin: 0;
  padding: 0;
}

.bbcode_container {
  width: 100%;
  max-width: 100%;
  margin: 0;
}
.bbcode_container img {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 0.35em 0;
  break-inside: avoid;
  page-break-inside: avoid;
}
.bbcode_container .speaker-portrait {
  display: inline-block;
  max-width: __PDF_SPEAKER_PORTRAIT_SIZE__;
  max-height: __PDF_SPEAKER_PORTRAIT_SIZE__;
  width: auto;
  height: auto;
  vertical-align: middle;
  margin: 0 0.35em 0.1em 0;
}
.long-image-slices {
  display: block;
  margin: 0.45em 0;
}
.long-image-slice {
  display: block;
  width: 100%;
  max-width: 100%;
  height: auto;
  margin: 0 0 4mm 0;
  break-inside: avoid;
  page-break-inside: avoid;
}
.long-image-slice:last-child {
  margin-bottom: 0;
}
blockquote {
  background: #ffffff;
  border: 1px solid #d6d6d6;
  border-left: 3px solid #bdbdbd;
  margin: 0.55em 0;
  padding: 0.45em 0.8em;
}
.skyblue   { color: skyblue; }
.royalblue { color: royalblue; }
.blue      { color: blue; }
.darkblue  { color: darkblue; }

.orange    { color: orange; }
.orangered { color: orangered; }
.crimson   { color: crimson; }
.red       { color: red; }
.firebrick { color: firebrick; }
.darkred   { color: darkred; }

.green     { color: green; }
.limegreen { color: limegreen; }
.seagreen  { color: seagreen; }
.teal      { color: teal; }

.deeppink  { color: deeppink; }
.tomato    { color: tomato; }
.coral     { color: coral; }

.purple    { color: purple; }
.indigo    { color: indigo; }

.burlywood  { color: burlywood; }
.sandybrown { color: sandybrown; }
.sienna     { color: sienna; }
.chocolate  { color: chocolate; }

.silver    { color: silver; }

em, i { font-style: italic; }
em *, i * { font-style: inherit; }
h2 { margin: 0 0 0.5em 0; page-break-after: avoid; }
hr { border: none; border-top: 1px solid #d6d6d6; margin: 0.9em 0; }
</style>
""".replace("__PDF_PAGE_SIZE__", PDF_PAGE_SIZE).replace(
  "__PDF_PAGE_MARGIN__", PDF_PAGE_MARGIN
).replace("__PDF_SPEAKER_PORTRAIT_SIZE__", PDF_SPEAKER_PORTRAIT_SIZE)