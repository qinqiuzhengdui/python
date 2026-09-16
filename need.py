import requests
def fetch(url: str,  save_path: str)->None:
    """
    从url下载文件并保存到save_path
    """
    
    headers={ 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.25 Safari/537.36 Core/1.70.3861.400 QQBrowser/10.7.4313.400'
    }

    try:
        response=requests.get(url, headers=headers, timeout=1500)
        response.raise_for_status()
        response.encoding=response.apparent_encoding
        print(response.status_code)
        print(response.text[:500])

        with open(save_path,"w",encoding="utf-8") as f:
            f.write(response.text)
    except requests.RequestException as e:
            print(f"请求失败：{e}")    


if __name__ == '__main__':
    fetch('https://www.diction-style.com/index/home?channel=20860','hello.html')


# 请使用以下命令来测试运行：
"""
python need.py
"""
