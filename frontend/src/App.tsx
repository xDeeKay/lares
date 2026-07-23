import { Header } from './components/Header';
import { SystemStats } from './components/SystemStats';
import { DiskStorage } from './components/DiskStorage';
import { Containers } from './components/Containers';
import './App.css';

function App() {
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
