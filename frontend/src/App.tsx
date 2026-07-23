import { Header } from './components/Header';
import { SystemStats } from './components/SystemStats';
import { DiskStorage } from './components/DiskStorage';
import { Containers } from './components/Containers';
import { PasswordGate } from './auth/PasswordGate';
import { useAuth } from './auth/AuthContext';
import './App.css';

function App() {
  const { status } = useAuth();

  if (status === 'loading') {
    return null;
  }
  if (status === 'setup_required') {
    return <PasswordGate mode="setup" />;
  }
  if (status === 'unauthenticated') {
    return <PasswordGate mode="login" />;
  }

  return (
    <>
      <Header />
      <main className="dashboard">
        <SystemStats />
        <DiskStorage />
        <Containers />
      </main>
    </>
  );
}

export default App;
