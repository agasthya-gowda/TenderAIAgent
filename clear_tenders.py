from database.db import get_conn
conn = get_conn()
conn.execute('DELETE FROM tenders')
conn.commit()
conn.close()
print('Cleared all tenders')