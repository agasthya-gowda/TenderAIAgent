from database.db import get_conn
conn = get_conn()
conn.execute('DELETE FROM tenders')
conn.execute('DELETE FROM ceo_searches')
conn.execute('DELETE FROM learned_patterns')
conn.commit()
conn.close()
print('Cleared everything - fresh start')