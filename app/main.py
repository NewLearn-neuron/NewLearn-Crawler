from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from app.services import crawl_recent_articles

app = FastAPI(debug=True)

# 스케줄러 초기화
scheduler = BackgroundScheduler()

# 최근 기사 크롤링 작업 추가, 8시간마다 실행행
scheduler.add_job(crawl_recent_articles, "interval", hours=8)  

# 즉시 실행
@app.on_event("startup")
async def startup_event():
    """애플리케이션 시작 시 크롤러를 즉시 실행합니다."""
    crawl_recent_articles()
    scheduler.start()

@app.on_event("shutdown")
def shutdown_event():
    """애플리케이션 종료 시 스케줄러를 종료합니다."""
    scheduler.shutdown()
    