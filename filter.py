import json
import pandas as pd

def filter_jobs(js):
    # 读取全部jsonl行，组装为DataFrame
    data_list = []
    with open(js, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                data = json.loads(line)
                data_list.append(data)

    df = pd.DataFrame(data_list)
    print(f"Loaded {len(df)} rows")

    # 显示头部数据（不显示pageno列），并可在notebook中交互显示
    pd.set_option('display.expand_frame_repr', False)
    pd.set_option('display.width', 200)

    df = df.drop(columns=["pageno", "pageRequestId", "sourceUrl","sourceUrl", "jobId", "companyId", "capturedAt","lat", "lon", "jobTermCode", "jobTags_json", "sesameLabels_json","property_json","funcType1","isAd"])

    # to html
    df.to_csv("yingjiesheng_jobs_filtered.csv", index=False, encoding="utf-8-sig")

    return df

if __name__ == "__main__":
    js = '/Users/zli142/Desktop/workbench_local/yingjiesheng_scraper/yingjiesheng_jobs_人力资源_山东.jsonl'
    df = filter_jobs(js)